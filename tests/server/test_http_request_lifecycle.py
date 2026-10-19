"""Native Quart request-correlation middleware registration contracts."""

from __future__ import annotations

import asyncio
import logging

import pytest
from quart.testing import WebsocketResponseError

from lib.app.app_factory import create_base_app
from lib.api_response import api_error
from lib.http_transport.request_lifecycle import register_request_lifecycle
from lib.log import req_id


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure_origin,error_type', [
    ('send', ConnectionResetError), ('send', BrokenPipeError),
    ('send', RuntimeError), ('handler', ConnectionResetError),
    ('receiver_cleanup', RuntimeError),
])
def test_owned_http_contains_only_peer_send_failures(failure_origin, error_type, caplog):
    from types import SimpleNamespace
    from lib.app.app_lifecycle import owned_asgi_http_connection_class

    async def scenario():
        receiver_started = asyncio.Event()
        receiver_joined = asyncio.Event()
        handler_joined = asyncio.Event()
        error = error_type('injected failure')

        class Connection(owned_asgi_http_connection_class()):
            def _create_request_from_scope(self, send):
                return None

            async def handle_messages(self, request, receive):
                receiver_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    receiver_joined.set()
                    if failure_origin == 'receiver_cleanup':
                        raise error

            async def handle_request(self, request, send):
                try:
                    await receiver_started.wait()
                    if failure_origin == 'handler':
                        raise error
                    await send({'type': 'http.response.body', 'body': b'response'})
                finally:
                    handler_joined.set()

        async def send(message):
            if failure_origin == 'receiver_cleanup':
                raise ConnectionResetError('peer disconnected')
            raise error

        connection = Connection(SimpleNamespace(logger=logging.getLogger('http-peer-test')), {})
        before = asyncio.all_tasks()
        if failure_origin == 'send' and error_type in (ConnectionResetError, BrokenPipeError):
            await connection(None, send)
        else:
            with pytest.raises(error_type) as caught:
                await connection(None, send)
            assert caught.value is error
        assert receiver_joined.is_set() and handler_joined.is_set()
        assert asyncio.all_tasks() == before

    with caplog.at_level(logging.DEBUG, logger='http-peer-test'):
        asyncio.run(scenario())
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_owned_http_parent_cancellation_joins_receiver_and_sender():
    from types import SimpleNamespace
    from lib.app.app_lifecycle import owned_asgi_http_connection_class

    async def scenario():
        sending = asyncio.Event()
        joined = set()

        class Connection(owned_asgi_http_connection_class()):
            def _create_request_from_scope(self, send):
                return None

            async def handle_messages(self, request, receive):
                try:
                    await asyncio.Event().wait()
                finally:
                    joined.add('receiver')

            async def handle_request(self, request, send):
                try:
                    await send({})
                finally:
                    joined.add('sender')

        async def send(message):
            sending.set()
            await asyncio.Event().wait()

        connection = Connection(SimpleNamespace(logger=logging.getLogger('http-peer-test')), {})
        before = asyncio.all_tasks()
        owner = asyncio.create_task(connection(None, send))
        await sending.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert joined == {'receiver', 'sender'}
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_execution_clock_includes_policy_delay_before_route_and_wall_jump(monkeypatch):
    from types import SimpleNamespace
    from quart import request
    from lib.http_transport import request_lifecycle as lifecycle
    from lib.agent_core import execution_clock as clocks

    ticks = [10_000_000_000]
    wall = [1000.0]
    probe = SimpleNamespace(time=lambda: wall[0],
                            monotonic_ns=lambda: ticks[0])
    monkeypatch.setattr(lifecycle, 'time', probe)
    monkeypatch.setattr(clocks, 'time', probe)
    app = create_base_app('request-execution-clock-test', {'TESTING': True})

    async def exercise():
        async with app.test_request_context('/execution'):
            await lifecycle.assign_request_id_and_log()
            # Auth/body policy and command preparation happen after ingress.
            ticks[0] += 175_000_000
            wall[0] = -5000.0
            clock = lifecycle.request_execution_clock()
            assert clock.origin_at_ms == 1_000_000
            assert clock.origin_monotonic_ns == 10_000_000_000
            assert request._start_monotonic == 10.0
            assert clock.now_ms() == 1_000_175
    asyncio.run(exercise())


def test_bare_route_assembly_starts_clock_without_mismatching_old_epoch():
    from lib.http_transport.request_lifecycle import request_execution_clock
    from lib.agent_core.execution_clock import ExecutionClock
    app = create_base_app('bare-request-clock-test', {'TESTING': True})

    async def exercise():
        async with app.test_request_context('/execution'):
            assert isinstance(request_execution_clock(), ExecutionClock)
    asyncio.run(exercise())


def test_request_id_is_adopted_echoed_and_context_is_cleared():
    app = create_base_app('request-lifecycle-test', {'TESTING': True})
    assert register_request_lifecycle(app) is True
    assert register_request_lifecycle(app) is False

    @app.get('/items/<item_id>')
    async def item(item_id):
        return {'item': item_id, 'request_id': req_id()}

    async def exercise():
        async with app.test_app():
            response = await app.test_client().get(
                '/items/private-identity',
                headers={'X-Request-ID': 'browser-request-42'},
            )
            payload = await response.get_json()
        return response, payload

    response, payload = asyncio.run(exercise())
    assert response.headers['X-Request-ID'] == 'browser-request-42'
    assert payload['request_id'] == 'browser-request-42'
    assert req_id() == ''


def test_browser_protocol_upgrade_does_not_duplicate_domain_warning(caplog):
    app = create_base_app('request-protocol-log-test', {'TESTING': True})
    register_request_lifecycle(app)

    @app.post('/api/browser/poll')
    async def browser_poll():
        return api_error(
            'Browser protocol upgrade required',
            status=426,
            code='browser_protocol_upgrade_required',
        )

    @app.post('/api/other')
    async def other_client_error():
        return api_error('Upgrade required', status=426)

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            return (
                await client.post('/api/browser/poll'),
                await client.post('/api/other'),
            )

    with caplog.at_level(logging.DEBUG, logger='server.lifecycle'):
        browser_response, other_response = asyncio.run(exercise())

    assert browser_response.status_code == 426
    assert other_response.status_code == 426
    response_records = [
        record for record in caplog.records if '← POST ' in record.getMessage()
    ]
    assert any(
        record.levelno == logging.DEBUG
        and '/api/browser/poll 426' in record.getMessage()
        for record in response_records
    )
    assert any(
        record.levelno == logging.WARNING
        and '/api/other 426' in record.getMessage()
        for record in response_records
    )
    assert not any(
        record.levelno >= logging.WARNING
        and '/api/browser/poll 426' in record.getMessage()
        for record in response_records
    )


def test_production_write_fence_rejects_mutation_until_storage_ready(monkeypatch):
    import lib.storage as storage

    app = create_base_app('request-storage-fence', {'TESTING': True})
    register_request_lifecycle(app)
    lifecycle = {'status': 'ready'}
    app.extensions['tofu_production_lifecycle'] = lifecycle

    @app.post('/api/write')
    async def write():
        return {'written': True}

    state = {'ready': False, 'state': 'restarting'}
    monkeypatch.setattr(storage, 'storage_status', lambda: dict(state))

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            fenced = await client.post('/api/write')
            liveness = await client.get('/api/write')
            state.update(ready=True, state='ready')
            accepted = await client.post('/api/write')
            lifecycle['status'] = 'stopping'
            stopping = await client.post('/api/write')
        return fenced, liveness, accepted, stopping

    fenced, liveness, accepted, stopping = asyncio.run(exercise())
    assert fenced.status_code == 503
    assert (await_json := asyncio.run(fenced.get_json()))['error']
    assert liveness.status_code == 405
    assert accepted.status_code == 200
    assert stopping.status_code == 503


def test_production_write_fence_keeps_recovery_endpoints_postable(monkeypatch):
    """A wedged database must not fence the endpoints that recover from it.

    Restart/shutdown/boot-retry and the human-approval decide leg are the
    operator's fix for exactly this state; their state is file- or
    process-based, never the fenced storage authority.
    """
    import lib.storage as storage

    app = create_base_app('request-storage-fence-recovery', {'TESTING': True})
    register_request_lifecycle(app)
    app.extensions['tofu_production_lifecycle'] = {'status': 'ready'}

    recovery_paths = (
        '/api/v4/boot/retry',
        '/api/v1/update/restart',
        '/api/v4/update/restart',
        '/api/v1/update/shutdown',
        '/api/v4/update/shutdown',
        '/api/v1/update/lifecycle-approvals/abc123/decide',
        '/api/v4/update/lifecycle-approvals/abc123/decide',
    )
    for path in recovery_paths:
        app.add_url_rule(path, endpoint='recovery:' + path,
                         view_func=lambda: {'reached': True}, methods=['POST'])

    @app.post('/api/write')
    async def write():
        return {'written': True}

    monkeypatch.setattr(
        storage, 'storage_status',
        lambda: {'ready': False, 'state': 'unavailable'})

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            return (
                [(path, (await client.post(path)).status_code)
                 for path in recovery_paths],
                (await client.post('/api/write')).status_code,
            )

    recovery_results, fenced_status = asyncio.run(exercise())
    assert [status for _, status in recovery_results] == [200] * len(
        recovery_paths), recovery_results
    assert fenced_status == 503


def test_distributed_preview_rejects_http_mutation_before_route_execution():
    app = create_base_app('distributed-preview-fence', {'TESTING': True})
    register_request_lifecycle(app, distributed_preview_read_only=True)
    calls = []

    @app.post('/api/write')
    async def write():
        calls.append('write')
        return {'written': True}

    async def exercise():
        async with app.test_app():
            return await app.test_client().post('/api/write')

    response = asyncio.run(exercise())
    body = asyncio.run(response.get_json())
    assert response.status_code == 503
    assert response.content_type == 'application/problem+json'
    assert response.headers['Retry-After'] == '3600'
    assert body['code'] == 'distributed_preview_read_only'
    assert calls == []


def test_distributed_preview_rejects_websocket_before_route_execution():
    app = create_base_app('distributed-preview-websocket', {'TESTING': True})
    register_request_lifecycle(app, distributed_preview_read_only=True)
    calls = []

    @app.websocket('/ws')
    async def ws():
        calls.append('websocket')

    async def exercise():
        async with app.test_app():
            with pytest.raises(WebsocketResponseError) as raised:
                async with app.test_client().websocket('/ws') as connection:
                    await connection.receive()
            return raised.value.response

    response = asyncio.run(exercise())
    body = asyncio.run(response.get_json())
    assert response.status_code == 503
    assert response.content_type == 'application/problem+json'
    assert body['code'] == 'distributed_preview_read_only'
    assert calls == []

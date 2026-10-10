"""A live push WebSocket must not hold Hypercorn's restart drain open.

The serving loop closes sockets before the ASGI lifespan runs shutdown hooks.
Use a private listening socket so this checks the real transport ordering
without touching a production port or storage authority.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket

import pytest
from lib.hypercorn_runtime import worker_serve
from hypercorn.config import Config, Sockets
from hypercorn.utils import wrap_app


pytestmark = [pytest.mark.api, pytest.mark.timeout(15)]


def test_connected_push_socket_drains_before_lifespan_shutdown(monkeypatch):
    from lib.agent_core.push import PushHub
    from lib.api_keys import AuthContext
    from lib.app_factory import create_base_app
    from lib.server_shutdown import wait_for_shutdown_trigger
    import routes.push as push

    hub = PushHub()
    monkeypatch.setattr(push, 'hub', hub)
    context = AuthContext(
        key_id='push-drain-test', owner_user_id=17,
        account_user_id='push-drain-test', scopes=frozenset({'chat'}),
    )
    monkeypatch.setattr(
        push, '_resolve_push_ws_auth',
        lambda *_args: (context, 'token'),
    )
    app = create_base_app('push-drain-transport-test', {'TESTING': True})
    app.register_blueprint(push.push_bp)

    async def scenario():
        listener = socket.socket()
        from tofu_db._loopback import bind_ephemeral_loopback
        bind_ephemeral_loopback(listener)
        listener.listen(32)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        config = Config()
        config.graceful_timeout = 1.0
        config.shutdown_timeout = 5.0
        shutdown_requested = asyncio.Event()

        async def trigger():
            await wait_for_shutdown_trigger(
                shutdown_requested,
                close_push_clients=hub.close_clients_for_shutdown,
            )

        server = asyncio.create_task(worker_serve(
            wrap_app(app, config.wsgi_max_body_size, 'asgi'), config,
            sockets=Sockets([], [listener], []),
            shutdown_trigger=trigger,
        ))
        client_writer = None
        try:
            for _ in range(100):
                try:
                    client_reader, client_writer = await asyncio.open_connection(
                        '127.0.0.1', port)
                    break
                except OSError:
                    await asyncio.sleep(0.02)
            else:
                raise AssertionError('private Hypercorn listener did not start')

            websocket_key = base64.b64encode(os.urandom(16)).decode('ascii')
            client_writer.write((
                'GET /api/push HTTP/1.1\r\n'
                f'Host: 127.0.0.1:{port}\r\n'
                'Upgrade: websocket\r\n'
                'Connection: Upgrade\r\n'
                f'Sec-WebSocket-Key: {websocket_key}\r\n'
                'Sec-WebSocket-Version: 13\r\n\r\n'
            ).encode('ascii'))
            await client_writer.drain()
            response_headers = await asyncio.wait_for(
                client_reader.readuntil(b'\r\n\r\n'), timeout=2)
            assert response_headers.startswith(b'HTTP/1.1 101 ')
            for _ in range(100):
                if hub.client_count == 1:
                    break
                await asyncio.sleep(0.01)
            assert hub.client_count == 1

            shutdown_requested.set()
            await asyncio.wait_for(server, timeout=4)
            assert hub.client_count == 0
        finally:
            if client_writer is not None:
                client_writer.close()
                await client_writer.wait_closed()
            if not server.done():
                server.cancel()
                try:
                    await asyncio.wait_for(server, timeout=2)
                except (asyncio.CancelledError, TimeoutError):
                    # The assertion failure is the test result; the private
                    # socket is closed below even if cancellation stalls.
                    pass
            listener.close()

    asyncio.run(scenario())

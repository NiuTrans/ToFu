"""Large WebSocket encoding preserves wire content, responsiveness and ownership."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import socket
import threading
from urllib.parse import urlsplit

import pytest
from hypercorn.config import Config, Sockets
from hypercorn.utils import wrap_app
from wsproto import WSConnection, ConnectionType
from wsproto.connection import Connection
from wsproto.events import AcceptConnection, BytesMessage, CloseConnection, Ping, Pong, Request, TextMessage
from wsproto.extensions import PerMessageDeflate

from lib.hypercorn_runtime import worker_serve
from tofu_db._loopback import bind_ephemeral_loopback


pytestmark = [pytest.mark.api, pytest.mark.timeout(15)]


class WireClient:
    """A real wsproto peer; no optional WebSocket client package is required."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.connection = WSConnection(ConnectionType.CLIENT)
        self.pending = deque()

    async def send_event(self, event):
        self.writer.write(self.connection.send(event))
        await self.writer.drain()

    async def event(self):
        while not self.pending:
            data = await self.reader.read(65536)
            if not data:
                raise ConnectionError('WebSocket peer closed')
            self.connection.receive_data(data)
            self.pending.extend(self.connection.events())
        return self.pending.popleft()

    async def recv(self):
        pieces = []
        while True:
            event = await self.event()
            if isinstance(event, (TextMessage, BytesMessage)):
                pieces.append(event.data)
                if event.message_finished:
                    return ('' if isinstance(event, TextMessage) else b'').join(pieces)
            elif isinstance(event, Ping):
                await self.send_event(event.response())
            elif isinstance(event, CloseConnection):
                raise ConnectionError(f'WebSocket message interrupted: {event.code} {event.reason}')


@asynccontextmanager
async def wire_client(url, compression='deflate'):
    address = urlsplit(url)
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    client = WireClient(reader, writer)
    try:
        extensions = [PerMessageDeflate()] if compression else []
        await client.send_event(Request(host=address.netloc, target='/', extensions=extensions))
        accepted = await client.event()
        assert isinstance(accepted, AcceptConnection)
        assert bool(accepted.extensions) == bool(compression)
        yield client
    finally:
        writer.close()
        await writer.wait_closed()


@asynccontextmanager
async def websocket_server(handler):
    started, stop = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                    started.set()
                else:
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        else:
            assert (await receive())['type'] == 'websocket.connect'
            await send({'type': 'websocket.accept'})
            await handler(receive, send)

    listener = socket.socket()
    bind_ephemeral_loopback(listener)
    listener.listen(8)
    listener.setblocking(False)
    address = listener.getsockname()
    config = Config()
    config.errorlog = None
    config.graceful_timeout = .2
    config.shutdown_timeout = 2
    server = asyncio.create_task(worker_serve(
        wrap_app(app, config.wsgi_max_body_size, 'asgi'), config,
        sockets=Sockets([], [listener], []), shutdown_trigger=stop.wait))
    try:
        await asyncio.wait_for(started.wait(), 3)
        yield f'ws://{address[0]}:{address[1]}/'
    finally:
        stop.set()
        await asyncio.wait_for(server, 3)
        listener.close()


@pytest.mark.parametrize('compression', [None, 'deflate'])
@pytest.mark.parametrize('payload', ['连接🙂' * 90_000, bytes(range(256)) * 4096],
                         ids=['unicode', 'binary'])
def test_large_message_preserves_content_while_encoding_off_the_serving_loop(
        monkeypatch, compression, payload):
    original = Connection.send
    sizes, final_observations, encoder_threads = [], [], []
    heartbeat = threading.Event()
    loop = None

    def note_yield():
        heartbeat.set()

    def record(connection, event):
        if isinstance(event, (TextMessage, BytesMessage)) and event.data != 'after':
            data = event.data.encode('utf-8') if isinstance(event.data, str) else event.data
            sizes.append(len(data))
            encoder_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(note_yield)
            progressed = heartbeat.wait(.5)
            if event.message_finished:
                final_observations.append(progressed)
        return original(connection, event)

    monkeypatch.setattr(Connection, 'send', record)

    async def handler(receive, send):
        field = 'text' if isinstance(payload, str) else 'bytes'
        await send({'type': 'websocket.send', field: payload})
        await send({'type': 'websocket.send', 'text': 'after'})
        await receive()

    async def scenario():
        nonlocal loop
        loop = asyncio.get_running_loop()
        serving_thread = threading.get_ident()
        async with websocket_server(handler) as url:
            async with wire_client(url, compression) as client:
                assert await client.recv() == payload
                assert await client.recv() == 'after'
                await client.send_event(Ping(payload=b'probe'))
                assert await client.event() == Pong(payload=b'probe')
        assert sizes == [len(payload.encode('utf-8') if isinstance(payload, str) else payload)]
        assert encoder_threads and serving_thread not in encoder_threads
        assert final_observations == [True], 'the loop must progress during large-message encoding'

    asyncio.run(scenario())


def test_concurrent_messages_and_control_ping_preserve_message_boundaries(monkeypatch):
    original = Connection.send
    controls = []

    def record(connection, event):
        if isinstance(event, Pong):
            controls.append(True)
        return original(connection, event)

    monkeypatch.setattr(Connection, 'send', record)
    payloads = ['first🙂' * 150_000, 'second译' * 150_000]

    async def handler(receive, send):
        await asyncio.gather(*[
            send({'type': 'websocket.send', 'text': payload}) for payload in payloads])
        await receive()

    async def scenario():
        async with websocket_server(handler) as url:
            async with wire_client(url) as client:
                await client.send_event(Ping(payload=b'during-message'))
                async with asyncio.timeout(3):
                    assert await client.recv() == payloads[0]
                    assert await client.recv() == payloads[1]
        assert controls, 'control frames must remain valid alongside concurrent messages'

    asyncio.run(scenario())


@pytest.mark.parametrize('encoder_fails', [False, True])
def test_cancelled_encoding_joins_the_worker_and_closes_before_a_later_send(monkeypatch, encoder_fails):
    original = Connection.send
    entered, release = threading.Event(), threading.Event()
    emitted = []

    def record(connection, event):
        if isinstance(event, TextMessage):
            emitted.append(event.data)
            entered.set()
            assert release.wait(3)
            if encoder_fails:
                raise RuntimeError('injected encoder failure')
        return original(connection, event)

    monkeypatch.setattr(Connection, 'send', record)

    async def scenario():
        settled = asyncio.Event()

        async def handler(receive, send):
            sending = asyncio.create_task(send({'type': 'websocket.send', 'text': 'large' * 400_000}))
            try:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                sending.cancel()
                await asyncio.sleep(0)
                sending.cancel()
                await asyncio.sleep(0)
                assert not sending.done(), 'cancellation must join the physical encoder'
                release.set()
                if encoder_fails:
                    with pytest.raises(BaseExceptionGroup) as caught:
                        await sending
                    assert caught.value.subgroup(asyncio.CancelledError)
                    assert caught.value.subgroup(RuntimeError)
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await sending
                await send({'type': 'websocket.send', 'text': 'must-not-send'})
                assert (await receive())['type'] == 'websocket.disconnect'
            finally:
                release.set()
                if not sending.done():
                    sending.cancel()
                    await asyncio.gather(sending, return_exceptions=True)
                settled.set()

        async with websocket_server(handler) as url:
            async with wire_client(url) as client:
                with pytest.raises(ConnectionError):
                    await client.recv()
                await settled.wait()
        assert emitted and 'must-not-send' not in emitted

    asyncio.run(scenario())


def test_http2_websocket_keeps_compression_content_and_flow_control():
    import base64
    import random
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, ResponseReceived

    payload = base64.b64encode(random.Random(27).randbytes(384 * 1024)).decode() + '中文🙂' * 1000

    async def handler(receive, send):
        await send({'type': 'websocket.send', 'text': payload})
        await receive()

    async def scenario():
        async with websocket_server(handler) as url:
            address = urlsplit(url)
            reader, writer = await asyncio.open_connection(address.hostname, address.port)
            h2 = H2Connection(H2Configuration(client_side=True))
            extension = PerMessageDeflate()
            extension.finalize('permessage-deflate')
            peer = Connection(ConnectionType.CLIENT, [extension])
            pieces = []
            acknowledged_bytes = 0
            accepted = finished = False
            try:
                h2.initiate_connection()
                h2.send_headers(1, [
                    (b':method', b'CONNECT'), (b':protocol', b'websocket'),
                    (b':scheme', b'http'), (b':authority', address.netloc.encode()),
                    (b':path', b'/'), (b'sec-websocket-version', b'13'),
                    (b'sec-websocket-extensions', b'permessage-deflate'),
                ])
                writer.write(h2.data_to_send())
                async with asyncio.timeout(5):
                    while not finished:
                        data = await reader.read(65536)
                        assert data, 'H2 connection ended before the message'
                        for event in h2.receive_data(data):
                            if isinstance(event, ResponseReceived):
                                headers = dict(event.headers)
                                assert headers[b':status'] == b'200'
                                assert b'permessage-deflate' in headers[b'sec-websocket-extensions']
                                accepted = True
                            elif isinstance(event, DataReceived):
                                h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                                acknowledged_bytes += event.flow_controlled_length
                                peer.receive_data(event.data)
                                for frame in peer.events():
                                    assert isinstance(frame, TextMessage)
                                    pieces.append(frame.data)
                                    finished = frame.message_finished
                        writer.write(h2.data_to_send())
                        await writer.drain()
                assert accepted and ''.join(pieces) == payload
                assert acknowledged_bytes > 65535, 'exercise actual H2 receive-window replenishment'
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(scenario())

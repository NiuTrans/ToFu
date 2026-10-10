"""Real owned sockets enforce transport grace before application shutdown."""

from __future__ import annotations

import asyncio
import base64
import errno
import http.client
import os
import socket
import ssl
import struct
import subprocess
import sys
import time

import pytest
from hypercorn.config import Config, Sockets
from hypercorn.utils import wrap_app

from lib.hypercorn_runtime import build_hypercorn_config, worker_serve
from tofu_db._loopback import bind_ephemeral_loopback


pytestmark = [pytest.mark.api, pytest.mark.timeout(10)]


@pytest.mark.parametrize('failure_kind', ['peer', 'grouped_peer', 'mixed', 'application_reset', 'cancel'])
def test_connection_owner_contains_only_proven_peer_failures(monkeypatch, failure_kind):
    from hypercorn.asyncio.tcp_server import TCPServer
    from lib.hypercorn_runtime import _OwnedTCPServer, _PeerDisconnected

    peer = _PeerDisconnected('peer closed')
    application = ConnectionResetError('application failure')
    cleanup = RuntimeError('cleanup failure')
    failures = {
        'peer': peer,
        'grouped_peer': ExceptionGroup('transport', [ExceptionGroup('nested', [peer])]),
        'mixed': ExceptionGroup('transport', [peer, cleanup]),
        'application_reset': application,
        'cancel': asyncio.CancelledError(),
    }

    async def upstream_run(self):
        raise failures[failure_kind]

    monkeypatch.setattr(TCPServer, 'run', upstream_run)
    connection = object.__new__(_OwnedTCPServer)
    if failure_kind in ('peer', 'grouped_peer'):
        asyncio.run(connection.run())
    elif failure_kind == 'mixed':
        with pytest.raises(ExceptionGroup) as caught:
            asyncio.run(connection.run())
        assert caught.value.exceptions == (cleanup,)
    else:
        with pytest.raises(type(failures[failure_kind])) as caught:
            asyncio.run(connection.run())
        assert caught.value is failures[failure_kind]


@pytest.mark.parametrize('body_size', [34097, 495316, 1024 * 1024])
@pytest.mark.parametrize('application_kind', ['asgi', 'quart'])
@pytest.mark.timeout(30)
def test_connection_close_delivers_complete_body_to_fresh_http_clients(body_size, application_kind):
    """Python drain can finish before the kernel delivers the final response."""
    async def scenario():
        started = asyncio.Event()
        stop = asyncio.Event()
        body = b'x' * body_size

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
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-length', str(len(body)).encode())]})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        if application_kind == 'quart':
            from quart import Quart, Response

            quart_app = Quart(__name__, static_folder=None)

            @quart_app.before_serving
            async def on_ready():
                started.set()

            @quart_app.get('/')
            async def response():
                return Response(body, content_type='application/json')

            app = quart_app

        listener = socket.socket()
        bind_ephemeral_loopback(listener)
        listener.listen(8)
        listener.setblocking(False)
        config = Config()
        config.errorlog = None
        server = asyncio.create_task(worker_serve(
            wrap_app(app, config.wsgi_max_body_size, 'asgi'), config,
            sockets=Sockets([], [listener], []), shutdown_trigger=stop.wait))

        def read_responses():
            for _ in range(8):
                connection = http.client.HTTPConnection(*listener.getsockname(), timeout=5)
                try:
                    connection.request('GET', '/', headers={'Connection': 'close'})
                    response = connection.getresponse()
                    assert response.status == 200
                    assert response.read() == body
                finally:
                    connection.close()

        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.to_thread(read_responses)
            deadline=asyncio.get_running_loop().time()+.5
            while any(getattr(task.get_coro(),'__qualname__','').endswith('_server_callback') for task in asyncio.all_tasks()):
                assert asyncio.get_running_loop().time()<deadline, 'closed peers retain connection tasks until keepalive expires'
                await asyncio.sleep(.01)
            assert not server.done()
        finally:
            stop.set()
            await asyncio.wait_for(server, 2)
            listener.close()

    asyncio.run(scenario())


def test_normal_close_waits_for_userspace_bytes_when_kernel_queue_is_empty(monkeypatch):
    from lib.hypercorn_runtime import _OwnedTCPServer

    class Writer:
        pending = 10000
        eof_sent = False
        closed = False

        @property
        def transport(self):
            return self

        def get_extra_info(self, _):
            return None

        def get_write_buffer_size(self):
            return self.pending

        def write_eof(self):
            self.eof_sent = True

        def close(self):
            assert self.pending == 0, 'normal close released still-buffered response bytes'
            self.closed = True

        async def wait_closed(self):
            pass

    class IdleTask:
        async def stop(self):
            pass

    monkeypatch.setattr('lib.hypercorn_runtime._pending_tcp_bytes', lambda _: 0)

    async def scenario():
        connection = object.__new__(_OwnedTCPServer)
        connection.writer = Writer()
        connection.idle_task = IdleTask()
        connection.config = Config()
        before = asyncio.all_tasks()
        task = asyncio.create_task(connection._close())
        await asyncio.sleep(0.03)
        assert connection.writer.eof_sent
        assert not task.done()
        assert not connection.writer.closed
        connection.writer.pending = 0
        await asyncio.wait_for(task, 0.5)
        assert connection.writer.closed
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize('value,expected', [('', 300), ('nan', 300), ('inf', 300),
                                        ('bad', 300), ('0', 300), ('0.1', 1),
                                        ('900', 900), ('100000', 86400)])
def test_stalled_write_policy_is_finite_and_bounded(value, expected):
    config = build_hypercorn_config('127.0.0.1', 4500, keep_alive_timeout=15,
                                    environ={'TOFU_HTTP_WRITE_STALL_SECS': value})
    assert config.write_stall_timeout == expected
    assert config.connection_close_timeout == expected


@pytest.mark.parametrize('ending', ['expiry', 'cancellation'])
def test_normal_close_reclaims_unacknowledged_peer_on_expiry_or_cancellation(monkeypatch, ending):
    from lib.hypercorn_runtime import _OwnedTCPServer

    class Writer:
        closed = False
        aborted = False

        @property
        def transport(self):
            return self

        def get_extra_info(self, _):
            return None

        def get_write_buffer_size(self):
            return 0

        def abort(self):
            self.aborted = True

        def write_eof(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            assert self.aborted, 'close must not flush an abandoned peer indefinitely'

    class IdleTask:
        stopped = False

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr('lib.hypercorn_runtime._pending_tcp_bytes', lambda _: 10000)

    async def scenario():
        connection = object.__new__(_OwnedTCPServer)
        connection.writer = Writer()
        connection.idle_task = IdleTask()
        connection.config = Config()
        connection.config.connection_close_timeout = 0.08
        before = asyncio.all_tasks()
        task = asyncio.create_task(connection._close())
        if ending == 'cancellation':
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 0.5)
        assert connection.writer.aborted
        assert connection.writer.closed
        assert connection.idle_task.stopped
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize('progress', ['buffer', 'acknowledgement'])
@pytest.mark.parametrize('operation', ['write', 'close'])
def test_slow_progress_survives_multiple_idle_budgets(progress, operation):
    from lib.hypercorn_runtime import _wait_for_write_progress

    class Transport:
        pending = 10000
        aborted = False

        def get_write_buffer_size(self):
            return self.pending

        def abort(self):
            self.aborted = True

    class Socket:
        acknowledged = 0

        def getsockopt(self, *_):
            return bytes(120) + struct.pack('=Q', self.acknowledged)

    class Writer:
        transport = Transport()
        sock = Socket()

        def get_extra_info(self, name):
            return self.sock if name == 'socket' else None

        async def complete(self):
            # A drain/close remains blocked for five idle budgets while the
            # reader continuously makes progress, including with a flat buffer.
            for _ in range(16):
                await asyncio.sleep(0.025)
                if progress == 'buffer':
                    self.transport.pending -= 1
                else:
                    self.sock.acknowledged += 1
            return operation

    if progress == 'acknowledgement' and (sys.platform != 'linux' or not hasattr(socket, 'TCP_INFO')):
        pytest.skip('Linux acknowledged-byte observation')

    async def scenario():
        writer = Writer()
        before = asyncio.all_tasks()
        result = await _wait_for_write_progress(writer, writer.complete, 0.08)
        assert result == operation
        assert not writer.transport.aborted
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_delayed_event_loop_checks_progress_before_expiring_peer():
    from lib.hypercorn_runtime import _wait_for_write_progress

    class Transport:
        pending = 10000

        def get_write_buffer_size(self):
            return self.pending

        def abort(self):
            pytest.fail('newly progressing peer was aborted by a stale timer')

    class Writer:
        transport = Transport()

        def get_extra_info(self, _):
            return None

        async def complete(self):
            await asyncio.sleep(0.01)
            # Deliberately inject a scheduler pause longer than the budget.
            time.sleep(0.12)
            self.transport.pending -= 1
            await asyncio.sleep(0.02)

    async def scenario():
        writer = Writer()
        await _wait_for_write_progress(writer, writer.complete, 0.05)

    asyncio.run(scenario())


@pytest.mark.parametrize('info', ['unavailable', 'old_kernel'])
def test_stopped_peer_expires_with_unavailable_tcp_progress(info):
    from lib.hypercorn_runtime import _PeerWriteStall, _wait_for_write_progress

    class Transport:
        aborted = False

        def get_write_buffer_size(self):
            return 10000

        def abort(self):
            self.aborted = True

    class Socket:
        def getsockopt(self, *_):
            if info == 'unavailable':
                raise OSError(errno.ENOPROTOOPT, 'not supported')
            return bytes(104)

        def setsockopt(self, *_):
            pass

    class Writer:
        transport = Transport()

        def get_extra_info(self, _):
            return Socket()

    async def scenario():
        writer = Writer()
        before = asyncio.all_tasks()
        with pytest.raises(_PeerWriteStall):
            await _wait_for_write_progress(writer, lambda: asyncio.sleep(5), 0.08)
        assert writer.transport.aborted
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_cancelled_write_joins_progress_observer():
    from lib.hypercorn_runtime import _wait_for_write_progress

    class Transport:
        def get_write_buffer_size(self):
            return 10000

    class Writer:
        transport = Transport()

        def get_extra_info(self, _):
            return None

    async def scenario():
        before = asyncio.all_tasks()
        task = asyncio.create_task(_wait_for_write_progress(Writer(), lambda: asyncio.sleep(5), 1))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_slow_real_reader_completes_response_beyond_idle_budget():
    async def scenario():
        started = asyncio.Event()
        stop = asyncio.Event()
        body = b'x' * (4 * 1024 * 1024)

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
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-length', str(len(body)).encode())]})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        bind_ephemeral_loopback(listener)
        listener.listen(8)
        listener.setblocking(False)
        config = Config()
        config.errorlog = None
        # Real TCP delayed ACKs can exceed subsecond synthetic deadlines.
        # Exercise the validated production minimum; fake-clock tests above
        # retain short deterministic expiry/progress fault injection.
        config.write_stall_timeout = 1.0
        config.connection_close_timeout = 1.0
        server = asyncio.create_task(worker_serve(
            wrap_app(app, config.wsgi_max_body_size, 'asgi'), config,
            sockets=Sockets([], [listener], []), shutdown_trigger=stop.wait))
        writer = None
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
        peer.bind(('127.0.0.2', 0))
        peer.setblocking(False)
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.get_running_loop().sock_connect(peer, listener.getsockname())
            reader, writer = await asyncio.open_connection(sock=peer, limit=1024)
            writer.write(b'GET /slow HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
            await writer.drain()
            assert b'200' in await reader.readuntil(b'\r\n\r\n')
            start = asyncio.get_running_loop().time()
            chunks = []
            while chunk := await asyncio.wait_for(reader.read(32768), 2):
                chunks.append(chunk)
                await asyncio.sleep(0.03)
            assert b''.join(chunks) == body
            assert asyncio.get_running_loop().time() - start > 3 * config.write_stall_timeout
            assert not server.done()
            if hasattr(socket, 'TCP_USER_TIMEOUT'):
                assert listener.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 0
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            else:
                peer.close()
            stop.set()
            await asyncio.wait_for(server, 2)
            listener.close()

    asyncio.run(scenario())


@pytest.mark.timeout(20)
def test_repeated_unread_peers_release_ports_without_server_restart():
    async def scenario():
        started = asyncio.Event()
        stop = asyncio.Event()
        finished = asyncio.Queue()
        peers = []

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
            try:
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                if scope['path'] == '/health':
                    await send({'type': 'http.response.body', 'body': b'healthy', 'more_body': False})
                else:
                    while True:
                        await send({'type': 'http.response.body', 'body': b'x' * 65536,
                                    'more_body': True})
            finally:
                finished.put_nowait(scope['path'])

        listener = socket.socket()
        bind_ephemeral_loopback(listener)
        listener.listen(32)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        config = Config()
        config.errorlog = None
        config.graceful_timeout = 0.1
        config.write_stall_timeout = 1.0
        config.connection_close_timeout = 1.0
        server = asyncio.create_task(worker_serve(
            wrap_app(app, config.wsgi_max_body_size, 'asgi'), config,
            sockets=Sockets([], [listener], []), shutdown_trigger=stop.wait))
        try:
            await asyncio.wait_for(started.wait(), 2)
            loop = asyncio.get_running_loop()
            for _ in range(8):
                peer = socket.socket()
                peers.append(peer)
                peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                peer.bind(('127.0.0.2', 0))
                peer.setblocking(False)
                local = peer.getsockname()
                await loop.sock_connect(peer, ('127.0.0.1', port))
                await loop.sock_sendall(peer, b'GET /stalled HTTP/1.1\r\nHost: localhost\r\n\r\n')
                assert await asyncio.wait_for(finished.get(), 2) == '/stalled'
                # Keep the unread client descriptor alive like a leaking
                # proxy. RST must release its source port without its help.
                for _ in range(40):
                    reuse = socket.socket()
                    try:
                        reuse.bind(local)
                        break
                    except OSError as error:
                        assert error.errno == errno.EADDRINUSE
                        await asyncio.sleep(0.01)
                    finally:
                        reuse.close()
                else:
                    pytest.fail('unread peer retained its local port after server reclamation')
                assert not server.done()
            reader, writer = await asyncio.open_connection(
                '127.0.0.1', port, local_addr=('127.0.0.2', 0))
            try:
                writer.write(b'GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                await writer.drain()
                assert b'healthy' in await asyncio.wait_for(reader.read(), 2)
                assert await asyncio.wait_for(finished.get(), 2) == '/health'
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            stop.set()
            for peer in peers:
                peer.close()
            await asyncio.wait_for(server, 2)
            listener.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('error', [
    TimeoutError(errno.ETIMEDOUT, 'peer timed out'),
    TimeoutError('application timeout'),
    OSError(errno.EIO, 'unexpected I/O failure'),
])
def test_transport_close_contains_only_peer_timeout(error):
    from lib.hypercorn_runtime import _OwnedTCPServer

    class Writer:
        closed = False

        def write_eof(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            raise error

    class IdleTask:
        stopped = False

        async def stop(self):
            self.stopped = True

    connection = object.__new__(_OwnedTCPServer)
    connection.writer = Writer()
    connection.idle_task = IdleTask()
    if error.errno == errno.ETIMEDOUT:
        asyncio.run(connection._close())
    else:
        with pytest.raises(type(error)) as raised:
            asyncio.run(connection._close())
        assert raised.value is error
    assert connection.writer.closed
    assert connection.idle_task.stopped


@pytest.mark.parametrize('connection', ['idle', 'stream', 'websocket', 'short', 'slow_reader'])
@pytest.mark.parametrize('tls', [False, True])
def test_shutdown_closes_owned_connections_before_lifespan(connection, tls, tmp_path):
    certificate = tmp_path / 'certificate.pem'
    private_key = tmp_path / 'private-key.pem'
    if tls:
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(private_key), '-out', str(certificate),
            '-subj', '/CN=localhost', '-days', '1',
        ], check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def scenario():
        started = asyncio.Event()
        request_started = asyncio.Event()
        request_finished = asyncio.Event()
        shutdown_requested = asyncio.Event()
        lifecycle = []

        async def app(scope, receive, send):
            if scope['type'] == 'lifespan':
                while True:
                    message = await receive()
                    if message['type'] == 'lifespan.startup':
                        await send({'type': 'lifespan.startup.complete'})
                        started.set()
                    elif message['type'] == 'lifespan.shutdown':
                        lifecycle.append('shutdown')
                        if connection != 'idle':
                            assert request_finished.is_set()
                        await send({'type': 'lifespan.shutdown.complete'})
                        return
            try:
                if scope['type'] == 'websocket':
                    assert (await receive())['type'] == 'websocket.connect'
                    await send({'type': 'websocket.accept'})
                else:
                    await send({
                        'type': 'http.response.start', 'status': 200, 'headers': [],
                    })
                    await send({
                        'type': 'http.response.body', 'body': b'started',
                        'more_body': True,
                    })
                request_started.set()
                if connection == 'slow_reader':
                    # A browser/proxy may stop reading a large response.
                    # Cancellation must not flush its buffered bytes forever.
                    while True:
                        await send({
                            'type': 'http.response.body', 'body': b'x' * 65536,
                            'more_body': True,
                        })
                elif connection == 'short':
                    await asyncio.sleep(0.03)
                    await send({
                        'type': 'http.response.body', 'body': b'completed',
                        'more_body': False,
                    })
                else:
                    # An active stream need not finish voluntarily. Its
                    # cancellation must happen before lifespan cleanup.
                    await asyncio.Event().wait()
            finally:
                request_finished.set()

        listener = socket.socket()
        bind_ephemeral_loopback(listener)
        listener.listen(32)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        config = Config()
        config.graceful_timeout = 0.15
        config.shutdown_timeout = 2.0
        config.errorlog = None
        if tls:
            config.certfile = str(certificate)
            config.keyfile = str(private_key)
            client_tls = ssl.create_default_context(cafile=str(certificate))
        else:
            client_tls = None
        server = asyncio.create_task(worker_serve(
            wrap_app(app, config.wsgi_max_body_size, 'asgi'), config,
            sockets=Sockets([listener], [], []) if tls else Sockets([], [listener], []),
            shutdown_trigger=shutdown_requested.wait,
        ))
        writer = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            reader, writer = await asyncio.open_connection(
                '127.0.0.1', port, ssl=client_tls, local_addr=('127.0.0.2', 0),
                server_hostname='localhost' if tls else None,
                limit=1024 if connection == 'slow_reader' else 65536,
            )
            if connection == 'slow_reader':
                writer.get_extra_info('socket').setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, 1024,
                )
            if connection != 'idle':
                headers = 'GET /stream HTTP/1.1\r\nHost: localhost\r\n'
                if connection == 'websocket':
                    key = base64.b64encode(os.urandom(16)).decode('ascii')
                    headers += (
                        'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                        f'Sec-WebSocket-Key: {key}\r\n'
                        'Sec-WebSocket-Version: 13\r\n'
                    )
                writer.write((headers + '\r\n').encode('ascii'))
                await writer.drain()
                await asyncio.wait_for(request_started.wait(), timeout=2)
                if connection == 'slow_reader':
                    await asyncio.sleep(0.1)
            shutdown_requested.set()
            # Keep the peer open: the server owns its connection deadline.
            done, _ = await asyncio.wait({server}, timeout=1.5)
            assert done, 'open peer bypassed the configured transport grace'
            await server
            assert lifecycle == ['shutdown']
            try:
                response = (b'' if connection == 'slow_reader' else
                            await asyncio.wait_for(reader.read(), timeout=1))
            except ConnectionResetError:
                assert connection in {'stream', 'websocket', 'slow_reader'}
                response = b''
            if connection == 'short':
                assert b'completed' in response
        finally:
            if writer is not None:
                writer.transport.abort()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    # Abandoned shutdown peers deliberately receive a reset.
                    assert connection in {'stream', 'websocket', 'slow_reader'}
            if not server.done():
                server.cancel()
                try:
                    await asyncio.wait_for(server, timeout=2)
                except (asyncio.CancelledError, TimeoutError):
                    # Failed scenarios still close their private listener;
                    # cancellation is expected after the timeout assertion.
                    pass
            listener.close()

    asyncio.run(scenario())


@pytest.mark.timeout(30)
def test_http2_disconnected_reader_reclaims_connection_before_keepalive(tmp_path):
    """Native SSE EOF must release layered producers and owned connection tasks."""
    from quart import jsonify
    from tofu_agent.server import HeadlessServerConfig,create_app
    from lib.server_tls import ensure_tls_certificates
    from benchmarks.http_stream_lifecycle import StreamingRuntime,TOKEN,check

    async def scenario():
        started=asyncio.Event();stop=asyncio.Event();runtime=StreamingRuntime()
        app=create_app(runtime=runtime,model_routing_setup=object(),
            config=HeadlessServerConfig(token=TOKEN,auth_mode='token',setup_enabled=False))
        @app.before_serving
        async def ready():started.set()
        @app.get('/benchmark/stream/<task_id>')
        async def diagnostics(task_id):
            execution=runtime.get(task_id)
            return jsonify({'active':execution.active,'closed':execution.closed,
                            'produced':execution.produced,'status':execution.status})
        listener=socket.socket();bind_ephemeral_loopback(listener);listener.listen(8);listener.setblocking(False)
        config=Config();config.errorlog=None;config.keep_alive_timeout=60;config.graceful_timeout=.1
        config.certfile,config.keyfile=ensure_tls_certificates(data_root=str(tmp_path),bind_host='127.0.0.1',environ={})
        server=asyncio.create_task(worker_serve(wrap_app(app,config.wsgi_max_body_size,'asgi'),config,
            sockets=Sockets([listener],[],[]),shutdown_trigger=stop.wait))
        try:
            await asyncio.wait_for(started.wait(),2)
            for _ in range(10):
                await check(f'https://127.0.0.1:{listener.getsockname()[1]}','h2',config.certfile)
            deadline=asyncio.get_running_loop().time()+.5
            while any(getattr(task.get_coro(),'__qualname__','').endswith('_server_callback') for task in asyncio.all_tasks()):
                assert asyncio.get_running_loop().time()<deadline,'H2 EOF leaves owned connection waiting for keepalive'
                await asyncio.sleep(.01)
            assert not server.done()
        finally:
            stop.set();await asyncio.wait_for(server,2);listener.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('idle',[True,False])
def test_late_idle_update_cannot_resurrect_closed_connection(idle):
    from hypercorn.events import Updated
    from lib.hypercorn_runtime import _OwnedTCPServer
    class IdleTimer:
        calls=0
        async def restart(self,*args):self.calls+=1
        async def stop(self):self.calls+=1
    async def scenario():
        connection=object.__new__(_OwnedTCPServer)
        connection._protocol_closed=False;connection._read_task=None
        connection._task_group=object();connection.idle_task=IdleTimer()
        connection._signal_close()
        await connection.protocol_send(Updated(idle=idle))
        assert connection.idle_task.calls==0,'late completion recreates a closed peer timer'
    asyncio.run(scenario())


def test_protocol_close_failure_still_retires_owned_idle_timer():
    from lib.hypercorn_runtime import _OwnedTCPServer
    class Reader:
        def at_eof(self):return True
    class Protocol:
        async def handle(self,event):raise RuntimeError('injected protocol close fault')
    class Timer:
        stopped=False
        async def stop(self):self.stopped=True
    async def scenario():
        connection=object.__new__(_OwnedTCPServer)
        connection.reader=Reader();connection.protocol=Protocol();connection.idle_task=Timer()
        connection._protocol_closed=False;connection._read_task=None
        with pytest.raises(RuntimeError,match='injected protocol close fault'):
            await connection._read_data()
        assert connection.idle_task.stopped,'protocol fault abandons owned timer'
        assert connection._protocol_closed,'EOF never published closing authority'
    asyncio.run(scenario())


@pytest.mark.parametrize('reason,closing,contained', [
    ('APPLICATION_DATA_AFTER_CLOSE_NOTIFY', True, True),
    ('APPLICATION_DATA_AFTER_CLOSE_NOTIFY', False, False),
    ('WRONG_VERSION_NUMBER', True, False),
])
def test_tls_peer_close_error_is_contained_after_owned_teardown(reason, closing, contained):
    from lib.hypercorn_runtime import _OwnedTCPServer
    class Timer:
        stopped=False
        async def stop(self):self.stopped=True
    class Writer:
        closed=False
        def get_extra_info(self, key):return None
        def write_eof(self):raise NotImplementedError('TLS')
        def close(self):self.closed=True
        def is_closing(self):return closing
        async def wait_closed(self):
            error=ssl.SSLError(1,'owned TLS close fixture');error.reason=reason;raise error
    async def scenario():
        connection=object.__new__(_OwnedTCPServer)
        connection.writer=Writer();connection.idle_task=Timer()
        if contained:await connection._close()
        else:
            with pytest.raises(ssl.SSLError):await connection._close()
        assert connection.writer.closed and connection.idle_task.stopped
    asyncio.run(scenario())


def test_late_h2_data_keeps_other_streams_and_connection_credit():
    from types import SimpleNamespace
    from h2.events import DataReceived, StreamEnded
    from hypercorn.protocol.h2 import H2Protocol
    from lib.hypercorn_runtime import _install_late_h2_data_guard
    async def scenario():
        protocol=object.__new__(H2Protocol)
        protocol.streams={3:object(),5:object()}
        credits=[];delivered=[];early_response=object()
        protocol.connection=SimpleNamespace(acknowledge_received_data=lambda length,stream:credits.append((length,stream)))
        async def upstream(events):
            for event in events:
                delivered.append(event)
                if event is early_response:protocol.streams.pop(3)
        protocol._handle_events=upstream
        original_class_handler=H2Protocol._handle_events
        wrapper=SimpleNamespace(protocol=protocol)
        _install_late_h2_data_guard(wrapper);handler=protocol._handle_events
        _install_late_h2_data_guard(wrapper);assert protocol._handle_events is handler
        late=DataReceived(stream_id=3,data=b'late',flow_controlled_length=4)
        live=DataReceived(stream_id=5,data=b'live',flow_controlled_length=4)
        ended=StreamEnded(stream_id=3)
        await protocol._handle_events([early_response,late,live,ended])
        assert delivered==[early_response,live,ended] and credits==[(4,3)]
        assert H2Protocol._handle_events is original_class_handler
    asyncio.run(scenario())

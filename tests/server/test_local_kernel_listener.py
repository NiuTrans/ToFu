"""Local-kernel listener: loopback only, ephemeral port, reported once listening.

A launcher on a phone cannot know which port is free for every app on the
device, so the local kernel asks the OS for one (port 0). The port is bound,
and therefore known, before the lifespan starts; the launching process is told
only once that listener accepts connections.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import threading

import pytest

import local_kernel

pytestmark = [pytest.mark.api, pytest.mark.timeout(60)]


def _listeners(sockets):
    return [*sockets.secure_sockets, *sockets.insecure_sockets]


def _connect_refused(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex((local_kernel.LOOPBACK_HOST, port)) != 0


async def _plain_app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'served'})


def _get(port: int) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(
        local_kernel.LOOPBACK_HOST, port, timeout=10)
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_ephemeral_loopback_port_is_reserved_before_serving():
    _config, sockets, port = local_kernel.bind_loopback(0)
    try:
        assert port > 0
        assert [listener.getsockname() for listener in _listeners(sockets)] == [
            ('127.0.0.1', port)]
        with socket.socket() as rival, pytest.raises(OSError):
            rival.bind(('127.0.0.1', port))
        assert _connect_refused(port), 'bound, but nothing is served before the lifespan'
    finally:
        for listener in _listeners(sockets):
            listener.close()


@pytest.mark.parametrize('port', [-1, 65536])
def test_ports_outside_the_tcp_range_are_refused_before_binding(port):
    with pytest.raises(ValueError, match='port'):
        local_kernel.bind_loopback(port)


def test_listening_is_reported_once_connections_are_accepted():
    from lib.hypercorn_runtime import serve

    config, sockets, port = local_kernel.bind_loopback(0)
    reports = []

    def on_listening(served):
        # A completed TCP handshake proves listen() already ran.
        with socket.create_connection((local_kernel.LOOPBACK_HOST, port), timeout=2):
            pass
        reports.append(served)

    async def exercise():
        stop = asyncio.Event()
        server = asyncio.create_task(serve(
            _plain_app, config, sockets=sockets, on_listening=on_listening,
            shutdown_trigger=stop.wait))
        while not reports and not server.done():
            await asyncio.sleep(0.01)
        try:
            return await asyncio.to_thread(_get, port)
        finally:
            stop.set()
            await server

    assert asyncio.run(exercise()) == (200, b'served')
    assert reports == [sockets]


def test_a_failing_listening_report_still_shuts_the_lifespan_down():
    from lib.hypercorn_runtime import serve

    config, sockets, port = local_kernel.bind_loopback(0)
    phases = []

    async def app(scope, receive, send):
        assert scope['type'] == 'lifespan'
        while True:
            message = await receive()
            phases.append(message['type'])
            if message['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return

    def on_listening(_served):
        raise BrokenPipeError('launcher went away')

    with pytest.raises(BrokenPipeError):
        asyncio.run(serve(app, config, sockets=sockets, on_listening=on_listening,
                          shutdown_trigger=asyncio.Event().wait))
    assert phases == ['lifespan.startup', 'lifespan.shutdown']
    assert _connect_refused(port)


def test_listening_record_is_one_machine_readable_line():
    line = local_kernel.listening_record(43567)

    assert '\n' not in line
    assert json.loads(line) == {
        'event': 'listening', 'host': '127.0.0.1', 'port': 43567,
        'pid': os.getpid(),
    }


def test_server_entry_and_local_kernel_share_one_serve_path(monkeypatch):
    import lib.hypercorn_runtime as transport
    import server

    captured = {}

    def register(target_app, **kwargs):
        captured['lifecycle'] = (target_app, kwargs)

    async def transport_serve(target_app, config, **kwargs):
        captured['transport'] = (target_app, config, kwargs)

    monkeypatch.setattr(server, 'register_server_runtime_lifecycle', register)
    monkeypatch.setattr(transport, 'serve', transport_serve)
    app, config, sockets = object(), object(), object()
    stop = threading.Event()

    def on_listening(_served):
        return None

    asyncio.run(server.serve_until_shutdown(
        app, config, host='127.0.0.1', port=43567, shutdown_requested=stop,
        sockets=sockets, on_listening=on_listening))

    lifecycle_app, lifecycle = captured['lifecycle']
    assert lifecycle_app is app
    assert (lifecycle['host'], lifecycle['port']) == ('127.0.0.1', 43567)
    assert lifecycle['shutdown_requested'] is stop
    served_app, served_config, served = captured['transport']
    assert (served_app, served_config) == (app, config)
    assert served['sockets'] is sockets
    assert served['on_listening'] is on_listening
    assert callable(served['shutdown_trigger'])

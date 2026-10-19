"""Owned TLS, explicit proxy and H2 peers attest actual native socket writes.

Build tofu-runtime's http_write_budget_probe example and supply its path through
TOFU_NATIVE_HTTP_WRITE_PROBE. The probe exercises the same reqwest connector as
native Transport; native HTTP/stdio tests own higher-level policy and Stop.
"""
import gzip
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.api


def read_headers(peer):
    data = bytearray()
    while not data.endswith(b'\r\n\r\n'):
        byte = peer.recv(1)
        assert byte, 'peer closed before request headers'
        data.extend(byte)
        assert len(data) <= 8192
    return data.decode('ascii')


@pytest.mark.parametrize('protocol,proxy,scenario', [
    ('http1', False, 'stalled_write'),
    ('http1', True, 'stalled_write'),
    ('tls1', False, 'stalled_write'),
    ('tls1', True, 'stalled_write'),
    ('h2', False, 'stalled_write'),
    ('h2', True, 'stalled_write'),
    ('tls1', False, 'slow_headers'),
    ('h2', False, 'slow_headers'),
    ('tls1', True, 'slow_connect'),
    ('h2', False, 'held_credit'),
    ('tls1', False, 'peer_reset'),
    ('h2', False, 'peer_reset'),
    ('tls1', False, 'recovery'),
    ('h2', False, 'recovery'),
])
def test_native_physical_write_budget(tmp_path, protocol, proxy, scenario):
    probe = os.environ.get('TOFU_NATIVE_HTTP_WRITE_PROBE')
    if not probe:
        pytest.skip('explicit native HTTP write probe build required')
    assert Path(probe).is_file()
    from lib.server.server_tls import ensure_tls_certificates
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, RequestReceived, StreamEnded, StreamReset
    from h2.settings import SettingCodes

    ca, key = ensure_tls_certificates(data_root=str(tmp_path), bind_host='127.0.0.1', environ={})
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, key)
    context.set_alpn_protocols(['h2' if protocol == 'h2' else 'http/1.1'])
    listener = socket.socket()
    listener.settimeout(6)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    secure = protocol != 'http1'
    endpoint = f'{"https" if secure else "http"}://127.0.0.1:{port}/fixture'
    release = threading.Event()
    observations = dict(requests=0, eof=False, received=0, resets=0)
    failures = []

    def serve(listener, scenario, observations, request_count=2):
        try:
            with listener, listener.accept()[0] as connection:
                connection.settimeout(6)
                if proxy and secure:
                    assert read_headers(connection).startswith(f'CONNECT 127.0.0.1:{port} HTTP/1.1\r\n')
                    connection.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                if scenario == 'slow_connect':
                    time.sleep(.15)  # Greater than write budget, below connect budget.
                peer = context.wrap_socket(connection, server_side=True) if secure else connection
                with peer:
                    if secure:
                        assert peer.selected_alpn_protocol() == ('h2' if protocol == 'h2' else 'http/1.1')
                    h2 = None
                    if protocol == 'h2':
                        h2 = H2Connection(config=H2Configuration(client_side=False, header_encoding='utf-8'))
                        h2.initiate_connection()
                        h2.update_settings({SettingCodes.INITIAL_WINDOW_SIZE:
                            0 if scenario == 'held_credit' else 64 << 20})
                        h2.increment_flow_control_window(64 << 20)
                        peer.sendall(h2.data_to_send())
                    if scenario in {'stalled_write', 'held_credit', 'peer_reset'}:
                        if h2 is None:
                            headers = read_headers(peer)
                            expected = endpoint if proxy and not secure else '/fixture'
                            assert headers.startswith(f'POST {expected} HTTP/1.1\r\n')
                            observations['requests'] += 1
                        else:
                            while not observations['requests']:
                                data = peer.recv(65536)
                                assert data
                                for event in h2.receive_data(data):
                                    if isinstance(event, RequestReceived):
                                        observations['requests'] += 1
                                    elif isinstance(event, DataReceived):
                                        observations['received'] += len(event.data)
                                peer.sendall(h2.data_to_send())
                        if scenario == 'peer_reset':
                            peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                            return
                        assert release.wait(6), 'probe did not report its independently owned timeout'
                    else:
                        for _ in range(request_count):
                            if h2 is None:
                                headers = read_headers(peer)
                                assert headers.startswith('POST /fixture HTTP/1.1\r\n')
                                remaining = int(next(line.split(':', 1)[1] for line in headers.splitlines()
                                    if line.lower().startswith('content-length:')))
                                while remaining:
                                    chunk = peer.recv(min(remaining, 65536))
                                    assert chunk
                                    remaining -= len(chunk)
                                stream_id = None
                            else:
                                stream_id = None
                                while stream_id is None:
                                    data = peer.recv(65536)
                                    assert data
                                    for event in h2.receive_data(data):
                                        if isinstance(event, DataReceived):
                                            h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                                        elif isinstance(event, StreamEnded):
                                            stream_id = event.stream_id
                                    peer.sendall(h2.data_to_send())
                            observations['requests'] += 1
                            if scenario == 'slow_headers':
                                time.sleep(.15)
                            body = gzip.compress(b'fixture response')
                            if h2 is None:
                                peer.sendall((f'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body)
                            else:
                                h2.send_headers(stream_id, [(':status','200'),('content-encoding','gzip'),('content-length',str(len(body)))])
                                h2.send_data(stream_id, body, end_stream=True)
                                peer.sendall(h2.data_to_send())
                    while True:
                        try:
                            data = peer.recv(65536)
                        except (ConnectionResetError, ssl.SSLEOFError):
                            data = b''
                        if not data:
                            observations['eof'] = True
                            break
                        observations['received'] += len(data)
                        if scenario == 'held_credit':
                            for event in h2.receive_data(data):
                                if isinstance(event, StreamReset):
                                    observations['resets'] += 1
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=serve, args=(listener,
        'stalled_write' if scenario == 'recovery' else scenario, observations))
    healthy = None
    healthy_worker = None
    healthy_observations = dict(requests=0, eof=False, received=0, resets=0)
    if scenario == 'recovery':
        healthy = socket.socket()
        healthy.settimeout(6)
        healthy.bind(('127.0.0.1', 0))
        healthy.listen(1)
        healthy_worker = threading.Thread(target=serve,
            args=(healthy, 'slow_headers', healthy_observations, 1))
        healthy_worker.start()
    worker.start()
    try:
        config = dict(endpoint=endpoint, ca_pem=Path(ca).read_text() if secure else None,
            proxy=f'http://127.0.0.1:{port}' if proxy else None,
            body_bytes=32 << 20 if scenario in {'stalled_write','held_credit','peer_reset','recovery'} else 2,
            requests=2 if scenario in {'slow_headers','slow_connect','recovery'} else 1,
            write_ms=80, deadline_ms=400 if scenario == 'held_credit' else 4000)
        if healthy is not None:
            config['recovery_endpoint'] = f'https://127.0.0.1:{healthy.getsockname()[1]}/fixture'
        completed = subprocess.run([probe], input=json.dumps(config), text=True,
            capture_output=True, timeout=8, check=True)
        rows = json.loads(completed.stdout)['requests']
    finally:
        release.set()
        worker.join(7)
        listener.close()
        if healthy_worker is not None:
            healthy_worker.join(7)
            healthy.close()
            assert not healthy_worker.is_alive(), 'healthy peer did not join'
    assert not worker.is_alive(), 'owned server did not join'
    assert not failures, failures
    assert observations['eof'] or scenario == 'peer_reset'
    assert observations['requests'] == (1 if scenario == 'recovery' else config['requests'])
    if scenario in {'stalled_write', 'recovery'}:
        assert rows[0] == dict(status='transport_error', write_timeout=True, timeout=True, connect=False), completed.stderr
        assert len(rows) == config['requests']
        if scenario == 'recovery':
            assert rows[1] == dict(status='done', version='HTTP/2.0' if protocol == 'h2' else 'HTTP/1.1', text='fixture response')
            assert healthy_observations['requests'] == 1 and healthy_observations['eof']
        assert observations['received'] < config['body_bytes']
    elif scenario == 'peer_reset':
        assert rows == [dict(status='transport_error', write_timeout=False, timeout=False, connect=False)], completed.stderr
    elif scenario == 'held_credit':
        assert rows == [dict(status='owner_deadline')], 'flow-control wait became a physical write timeout'
        assert observations['received'] < 128 << 10, 'body was not held at the advertised stream credit boundary'
    else:
        assert rows == [dict(status='done', version='HTTP/2.0' if protocol == 'h2' else 'HTTP/1.1',
            text='fixture response')] * 2

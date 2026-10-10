"""Public storage boundary: portable imports, handshake fences and adapters."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from lib.storage.client import StorageClient
from lib.storage.port import StoragePort
from lib.storage.v2_client import StorageV2Client
from tofu_db import StorageV2Error, StorageV2ProtocolError, StorageV2Session
from tofu_db import wire

pytestmark = pytest.mark.unit


class Socket:
    def __init__(self):
        self.closed = False
        self.sent = None

    def setsockopt(self, level, option, value):
        assert (level, option, value) == (wire.socket.IPPROTO_TCP, wire.socket.TCP_NODELAY, 1)

    def settimeout(self, timeout):
        pass

    def sendall(self, frame):
        self.sent = wire.decode_message(frame[4:-4])

    def close(self):
        self.closed = True


@pytest.mark.parametrize('failure', ['correlation', 'schema', 'missing_schema', 'rejected', 'eof', 'send', 'socket_option'])
def test_failed_handshake_closes_connection(monkeypatch, failure):
    sock = Socket()
    monkeypatch.setattr(wire.socket, 'create_connection', lambda *a, **kw: sock)

    def response(_):
        if failure == 'eof':
            raise EOFError('peer closed')
        value = {
            wire.FIELD_KIND: wire.KIND_RESPONSE,
            wire.FIELD_CORRELATION_ID: sock.sent[wire.FIELD_CORRELATION_ID],
            wire.FIELD_STATUS: 0,
            wire.FIELD_SCHEMA_ID: wire.STORAGE_SCHEMA_VERSION,
        }
        if failure == 'correlation': value[wire.FIELD_CORRELATION_ID] = b'wrong'
        if failure == 'schema': value[wire.FIELD_SCHEMA_ID] = 999999
        if failure == 'missing_schema': del value[wire.FIELD_SCHEMA_ID]
        if failure == 'rejected': value[wire.FIELD_STATUS] = 1
        return value

    monkeypatch.setattr(wire, 'recv_frame', response)
    if failure == 'socket_option':
        def fail_option(*args): raise OSError('socket option failed')
        sock.setsockopt = fail_option
    if failure == 'send':
        def send(_): raise OSError('broken pipe')
        sock.sendall = send
    with pytest.raises((StorageV2ProtocolError, StorageV2Error, EOFError, OSError)):
        StorageV2Session('localhost', 1234, b'x' * 32, owner_id=1, tenant_id=2)
    assert sock.closed


def test_successful_handshake_has_explicit_lifetime(monkeypatch):
    sock = Socket()
    monkeypatch.setattr(wire.socket, 'create_connection', lambda *a, **kw: sock)
    monkeypatch.setattr(wire, 'recv_frame', lambda _: {
        wire.FIELD_KIND: wire.KIND_RESPONSE,
        wire.FIELD_CORRELATION_ID: sock.sent[wire.FIELD_CORRELATION_ID],
        wire.FIELD_STATUS: 0, wire.FIELD_SCHEMA_ID: wire.STORAGE_SCHEMA_VERSION,
    })
    with StorageV2Session('localhost', 1234, b'x' * 32, owner_id=1, tenant_id=2) as session:
        assert session.schema_id == wire.STORAGE_SCHEMA_VERSION
        assert not sock.closed
    assert sock.closed


@pytest.mark.parametrize('scope', [dict(owner_id=True, tenant_id=1), dict(owner_id=1, tenant_id=-1), dict(owner_id=0, tenant_id=None)])
def test_invalid_scope_never_connects(monkeypatch, scope):
    def connect(*a, **kw): pytest.fail('invalid scope opened a socket')
    monkeypatch.setattr(wire.socket, 'create_connection', connect)
    with pytest.raises(ValueError):
        StorageV2Session('localhost', 1234, b'x' * 32, **scope)


def test_both_adapters_implement_public_port():
    clients = [StorageClient('localhost', 1234, 't' * 48),
               StorageV2Client('localhost', 1234, b'x' * 32, owner_id=1, tenant_id=2)]
    for client in clients:
        assert isinstance(client, StoragePort)
        for name in ('query', 'command', 'health', 'metrics', 'maintenance'):
            assert inspect.signature(getattr(type(client), name)) == inspect.signature(getattr(StoragePort, name))


def test_sdk_has_no_host_imports():
    for path in Path(wire.__file__).parent.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                       else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.split('.')[0] in {'lib', 'scripts', 'tofu_agent'} for name in modules)


@pytest.mark.parametrize('options', [
    {'secret': b'short'}, {'schema_ids': []}, {'schema_ids': [0]},
    {'schema_ids': [True]}, {'schema_ids': [2**32]},
])
def test_invalid_handshake_input_never_connects(monkeypatch, options):
    def connect(*args, **kwargs):
        pytest.fail('invalid Hello input opened a socket')
    monkeypatch.setattr(wire.socket, 'create_connection', connect)
    arguments = dict(secret=b'x' * 32, owner_id=1, tenant_id=2)
    arguments.update(options)
    with pytest.raises(ValueError):
        StorageV2Session('localhost', 1234, **arguments)

"""Port pressure recovery preserves endpoint, authority, and dispatch boundaries."""

import errno
import socket
import urllib.error
import urllib.request

import pytest

from tofu_db import _loopback as transport

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('error_number', [errno.EADDRINUSE, errno.EADDRNOTAVAIL])
def test_local_allocation_recovers_before_dispatch(monkeypatch, error_number):
    calls = []
    connected = object()

    def connect(address, **options):
        calls.append((address, options))
        if len(calls) == 1:
            raise OSError(error_number, 'allocation failed')
        return connected

    monkeypatch.setattr(transport.sys, 'platform', 'linux')
    monkeypatch.setattr(socket, 'create_connection', connect)
    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(transport.time, 'monotonic', lambda: next(ticks))
    assert transport.create_connection(('127.0.0.1', 4500), timeout=2) is connected
    assert calls == [(('127.0.0.1', 4500), {'timeout': 2}),
                     (('127.0.0.1', 4500), {'timeout': 1.75, 'source_address': ('127.0.0.2', 0)})]


@pytest.mark.parametrize('address,error_number,source', [
    ('example.com', errno.EADDRNOTAVAIL, None),
    ('127.0.0.1', errno.ECONNREFUSED, None),
    ('127.0.0.1', errno.EADDRNOTAVAIL, ('127.0.0.3', 0)),
])
def test_other_failures_and_explicit_sources_are_never_replayed(monkeypatch, address, error_number, source):
    calls = []
    error = OSError(error_number, 'failure')

    def connect(*args, **options):
        calls.append(options)
        raise error

    monkeypatch.setattr(socket, 'create_connection', connect)
    with pytest.raises(OSError) as raised:
        transport.create_connection((address, 4500), timeout=2, source_address=source)
    assert raised.value is error
    assert len(calls) == 1


def test_expired_budget_does_not_start_recovery(monkeypatch):
    calls = []

    def connect(*args, **options):
        calls.append(options)
        raise OSError(errno.EADDRNOTAVAIL, 'allocation failed')

    monkeypatch.setattr(transport.sys, 'platform', 'linux')
    monkeypatch.setattr(socket, 'create_connection', connect)
    ticks = iter([10.0, 12.0])
    monkeypatch.setattr(transport.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError):
        transport.create_connection(('127.0.0.1', 4500), timeout=1)
    assert len(calls) == 1


def test_listener_recovers_atomically_and_keeps_original_address(monkeypatch):
    monkeypatch.setattr(transport.sys, 'platform', 'linux')
    calls = []

    class Listener:
        def bind(self, address):
            calls.append(address)
            if len(calls) < 3:
                raise OSError(errno.EADDRINUSE, 'busy')

    transport.bind_ephemeral_loopback(Listener())
    assert calls[0] == ('127.0.0.1', 0)
    low, high = map(int, transport.Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split())
    assert all(host == '127.0.0.1' and not low <= port <= high for host, port in calls[1:])


def test_listener_exhaustion_is_bounded(monkeypatch):
    monkeypatch.setattr(transport.sys, 'platform', 'linux')
    calls = []

    class Listener:
        def bind(self, address):
            calls.append(address)
            raise OSError(errno.EADDRINUSE, 'busy')

    with pytest.raises(OSError) as raised:
        transport.bind_ephemeral_loopback(Listener())
    assert raised.value.errno == errno.EADDRINUSE
    assert len(calls) == 129


@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('status', [200, 503, 302])
def test_http_recovery_keeps_method_body_auth_and_tls(monkeypatch, scheme, status):
    monkeypatch.setattr(transport.sys, 'platform', 'linux')
    def exhausted(*args, **options):
        raise urllib.error.URLError(OSError(errno.EADDRNOTAVAIL, 'allocation failed'))
    monkeypatch.setattr(urllib.request, 'urlopen', exhausted)
    observations = {}
    tls_context = object()

    class Response:
        headers = {}
        reason = 'status'
        def close(self):
            observations['response_closed'] = True

    response = Response()
    response.status = status

    class Connection:
        def __init__(self, host, port, **options):
            observations.update(host=host, port=port, options=options)
        def connect(self):
            if 'source_address' not in observations['options']:
                raise OSError(errno.EADDRNOTAVAIL, 'allocation failed')
        def request(self, method, path, **options):
            observations.update(method=method, path=path, request=options)
        def getresponse(self):
            return response
        def close(self):
            observations['connection_closed'] = True

    monkeypatch.setattr(transport.http.client, 'HTTPConnection', Connection)
    monkeypatch.setattr(transport.http.client, 'HTTPSConnection', Connection)
    request = urllib.request.Request(f'{scheme}://127.0.0.1:4500/control?q=1',
                                    data=b'{"action":"repair"}', headers={'Authorization': 'Bearer synthetic'})
    if status >= 300:
        with pytest.raises(urllib.error.HTTPError) as raised:
            transport.urlopen(request, timeout=3, context=tls_context)
        assert raised.value.code == status
        raised.value.close()
    else:
        transport.urlopen(request, timeout=3, context=tls_context).close()
    assert observations['host'] == '127.0.0.1'
    assert observations['port'] == 4500
    assert observations['options']['source_address'] == ('127.0.0.2', 0)
    assert 0 < observations['options']['timeout'] <= 3
    if scheme == 'https':
        assert observations['options']['context'] is tls_context
    assert observations['method'] == 'POST'
    assert observations['path'] == '/control?q=1'
    assert observations['request']['body'] == request.data
    assert observations['request']['headers']['Authorization'] == 'Bearer synthetic'
    assert observations['connection_closed']
    assert observations['response_closed']


@pytest.mark.parametrize('stage', ['dispatch', 'response'])
def test_mutating_control_failure_after_connect_is_never_replayed(monkeypatch, stage):
    calls = []
    error = OSError(errno.EADDRNOTAVAIL, 'post-dispatch failure')

    class Connection:
        def __init__(self, *args, **options):
            calls.append('created')
        def connect(self):
            calls.append('connected')
        def request(self, *args, **options):
            calls.append('dispatched')
            if stage == 'dispatch':
                raise error
        def getresponse(self):
            raise error
        def close(self):
            calls.append('closed')

    monkeypatch.setattr(transport.http.client, 'HTTPConnection', Connection)
    request = urllib.request.Request('http://127.0.0.1:4500/control', data=b'{}')
    with pytest.raises(OSError) as raised:
        transport.urlopen(request, timeout=2)
    assert raised.value is error
    assert calls == ['created', 'connected', 'dispatched', 'closed']

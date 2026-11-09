#!/usr/bin/env python3
"""A slow upstream never convicts the egress path.

Root cause this guards (2026-10-21 boot): ``http_request`` reported EVERY
exception as a path failure. A pool entry owns a consecutive-failure budget
(2 fails → 60s cooldown), so on a deployment whose only egress is a single
pool entry, one slow upstream took the whole path out of service: the entry
cooled down, ``proxies_for`` then offered no proxy, and every other host fell
back to a direct path that resolves nothing on this class of host. The boot
window then showed an egress outage (self-update DNS failure, Artificial
Analysis read timeouts, registry probes) that had nothing to do with egress.

A response read timeout is evidence about the upstream, never about the
path. A timeout during proxy CONNECT or TLS is a connection failure, even
when urllib3 initially calls it a read timeout.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
        tests/proxy/test_read_timeout_does_not_convict_path.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
import requests

pytestmark = pytest.mark.unit


class _RecordingReport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, ok, latency_ms=None, **kwargs):
        self.calls.append((url, ok))


@pytest.fixture
def reporting(monkeypatch):
    """Intercept the transport's path verdict without touching the network."""
    from lib.http_transport import client

    recorder = _RecordingReport()
    from lib import proxy
    monkeypatch.setattr(proxy, 'report_network_route',
                        lambda url, route, ok, latency_ms=None, **kw: recorder(url, ok, latency_ms))
    return client, recorder


def _request_raising(client, monkeypatch, error):
    def boom(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', boom)
    return client.http_request('GET', 'https://example.invalid/x', timeout=1, use_proxy=False)


def test_read_timeout_is_not_charged_to_the_egress_path(reporting, monkeypatch):
    client, recorder = reporting
    with pytest.raises(requests.exceptions.ReadTimeout):
        _request_raising(
            client, monkeypatch, requests.exceptions.ReadTimeout('slow body'))
    assert recorder.calls == [], (
        'a read timeout convicted the egress path: %r' % (recorder.calls,))


def test_connect_timeout_still_convicts(reporting, monkeypatch):
    client, recorder = reporting
    with pytest.raises(requests.exceptions.ConnectTimeout):
        _request_raising(
            client, monkeypatch, requests.exceptions.ConnectTimeout('no route'))
    assert recorder.calls and recorder.calls[-1][1] is False


def test_proxy_error_still_convicts(reporting, monkeypatch):
    client, recorder = reporting
    with pytest.raises(requests.exceptions.ProxyError):
        _request_raising(
            client, monkeypatch, requests.exceptions.ProxyError('407'))
    assert recorder.calls and recorder.calls[-1][1] is False


def test_success_reports_a_healthy_path(reporting, monkeypatch):
    client, recorder = reporting

    response = requests.Response()
    response.status_code = 200
    response._content = b'ok'
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send',
                        lambda *_a, **_k: response)
    client.http_request('GET', 'https://example.invalid/x', timeout=1, use_proxy=False)
    assert recorder.calls == [('https://example.invalid/x', True)]


@pytest.mark.parametrize('proxy_tcp_connected', [False, True])
@pytest.mark.parametrize('timeout_kind', ['socket', 'urllib3_read'])
def test_proxy_setup_timeout_is_not_a_response_read(
        reporting, monkeypatch, proxy_tcp_connected, timeout_kind):
    from urllib3.connection import HTTPSConnection
    from urllib3.exceptions import ReadTimeoutError
    from lib.network_routes import is_safe_connect_failure

    client, recorder = reporting
    monkeypatch.delattr(client._sync_local, 'session', raising=False)
    sent = Mock(side_effect=AssertionError('application bytes must not be sent'))
    monkeypatch.setattr(HTTPSConnection, 'send', sent)

    def connect(connection):
        connection._has_connected_to_proxy = proxy_tcp_connected
        if timeout_kind == 'urllib3_read':
            raise ReadTimeoutError(None, '/', 'synthetic CONNECT wait')
        raise TimeoutError('synthetic CONNECT wait')

    monkeypatch.setattr(HTTPSConnection, 'connect', connect)
    try:
        with pytest.raises(requests.exceptions.ConnectionError) as caught:
            client.http_request('POST', 'https://example.invalid/x', data=b'body',
                                proxies={'https': 'http://proxy.invalid:8080'})
        assert is_safe_connect_failure(caught.value)
        assert recorder.calls == [('https://example.invalid/x', False)]
        sent.assert_not_called()
    finally:
        client.close_sync_http_clients()


def test_timeout_after_sending_request_keeps_response_read_semantics(reporting, monkeypatch):
    from urllib3.connection import HTTPSConnection
    from lib.network_routes import is_safe_connect_failure

    client, recorder = reporting
    monkeypatch.delattr(client._sync_local, 'session', raising=False)
    socket = Mock()
    socket.gettimeout.return_value = 10
    socket.makefile.side_effect = TimeoutError('synthetic response read wait')

    def connect(connection):
        connection.sock = socket
        connection.is_verified = True

    monkeypatch.setattr(HTTPSConnection, 'connect', connect)
    try:
        with pytest.raises(requests.exceptions.ReadTimeout) as caught:
            client.http_request('POST', 'https://example.invalid/x', data=b'body',
                                proxies={'no_proxy': '*'})
        assert not is_safe_connect_failure(caught.value)
        assert recorder.calls == []
        assert b'body' in b''.join(call.args[0] for call in socket.sendall.call_args_list)
    finally:
        client.close_sync_http_clients()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))

"""Transport failures retain phase and never turn upload failures into dead keys."""

import asyncio
import contextlib
import ssl
from unittest.mock import Mock

import httpx
import pytest
import requests
from urllib3.exceptions import NewConnectionError, ProtocolError, ReadTimeoutError

from lib.error_envelope import from_exception
from lib.llm._transport import classify_request_transport_error
from lib.llm.llm_errors import (
    EndpointUnreachableError, RequestTransportError, RequestUploadTimeoutError,
)

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]
URL = 'https://model.example/v1/messages'
BODY = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hello'}]}


def wrapped_write_timeout():
    prepared = requests.Request('POST', URL, data=b'x' * 4096).prepare()
    return requests.exceptions.ConnectionError(
        ProtocolError('Connection aborted.', TimeoutError('The write operation timed out')),
        request=prepared)


def test_wrapped_tls_write_timeout_retains_bytes_and_budget():
    error = classify_request_transport_error(
        wrapped_write_timeout(), base_url=URL, write_timeout=60)
    assert isinstance(error, RequestUploadTimeoutError)
    envelope = from_exception(error, model='test-model')
    assert envelope['kind'] == 'timeout'
    assert envelope['failureStage'] == 'upload'
    assert envelope['requestBytes'] == 4096
    assert envelope['timeoutSeconds'] == 60
    assert envelope['transportCause'] == 'TimeoutError'
    assert '正文上传超时' in envelope['message']
    assert 'faster model' not in envelope['hint']
    assert 'unreachable' not in envelope['detail']


@pytest.mark.parametrize('source,stage,kind', [
    (requests.exceptions.ConnectTimeout('connect'), 'connect', 'endpoint_unreachable'),
    (requests.exceptions.ConnectionError(NewConnectionError(None, 'refused')),
     'connect', 'endpoint_unreachable'),
    (requests.exceptions.ReadTimeout('read'), 'read', 'timeout'),
    (requests.exceptions.ConnectionError(ReadTimeoutError(None, '/', 'read')),
     'read', 'timeout'),
    (requests.exceptions.SSLError('certificate'), 'tls', 'network'),
    (requests.exceptions.ConnectionError('ambiguous disconnect'), 'connection', 'network'),
    (httpx.WriteTimeout('write', request=httpx.Request('POST', URL, content=b'abc')),
     'upload', 'timeout'),
    (httpx.PoolTimeout('pool'), 'pool', 'timeout'),
    (httpx.ConnectTimeout('connect'), 'connect', 'endpoint_unreachable'),
    (httpx.ConnectError('refused'), 'connect', 'endpoint_unreachable'),
    (httpx.ReadTimeout('read'), 'read', 'timeout'),
    (requests.exceptions.Timeout('timeout'), 'connection', 'timeout'),
    (httpx.TimeoutException('timeout'), 'connection', 'timeout'),
])
def test_failure_phases(source, stage, kind):
    error = classify_request_transport_error(source, base_url=URL)
    assert getattr(error, 'failure_stage', 'connect') == stage
    assert from_exception(error)['kind'] == kind


def test_httpx_certificate_error_is_tls_not_dead_endpoint():
    source = httpx.ConnectError('certificate')
    source.__cause__ = ssl.SSLCertVerificationError('certificate rejected')
    error = classify_request_transport_error(source, base_url=URL)
    assert isinstance(error, RequestTransportError)
    assert error.failure_stage == 'tls'


def test_transport_diagnostics_omit_credentials_and_query():
    error = classify_request_transport_error(
        requests.exceptions.SSLError('do not expose secret-key'),
        base_url='https://user:secret-key@model.example/v1/messages?token=secret-key')
    envelope = from_exception(error)
    assert 'secret-key' not in str(envelope)
    assert error.base_url == URL


def test_cyclic_exception_chain_is_bounded():
    source = requests.exceptions.ConnectionError('disconnected')
    source.__cause__ = source
    assert classify_request_transport_error(source).failure_stage == 'connection'


def test_read_timeout_preserves_bounded_retry_semantics():
    from lib.llm.llm_errors import RetryableAPIError
    error = classify_request_transport_error(requests.exceptions.ReadTimeout('read'))
    assert isinstance(error, RetryableAPIError)
    assert from_exception(error)['kind'] == 'timeout'


@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('fresh', [True, False])
def test_sync_connect_and_upload_budgets_are_independent(scheme, fresh):
    from lib.llm._sync_http import model_http_adapter

    adapter = model_http_adapter(write_timeout=60)
    connection = adapter.poolmanager.connection_from_url(scheme + '://model.example')._new_conn()
    socket = Mock()
    socket.gettimeout.return_value = 10
    observed = []
    socket.sendall.side_effect = lambda data: observed.append(connection.timeout)
    connection.timeout = 10
    connection.sock = None if fresh else socket

    def connect():
        observed.append(('connect', connection.timeout))
        connection.sock = socket

    connection.connect = connect
    connection.request('POST', '/v1/messages', body=b'hello')
    assert all(v == 60 for v in observed if not isinstance(v, tuple))
    assert ('connect', 10) in observed if fresh else ('connect', 10) not in observed
    assert connection.timeout == 10
    connection.close()
    adapter.close()


def test_upload_failure_is_typed_and_discards_connection():
    from lib.llm._sync_http import model_http_adapter

    adapter = model_http_adapter(write_timeout=60)
    connection = adapter.poolmanager.connection_from_url(URL)._new_conn()
    socket = Mock()
    connection.sock = socket
    connection.timeout = 10
    socket.sendall.side_effect = TimeoutError('write stalled')
    with pytest.raises(RequestUploadTimeoutError) as caught:
        connection.request('POST', '/v1/messages', body=b'hello')
    assert caught.value.request_bytes == 5
    assert caught.value.timeout_seconds == 60
    assert connection.timeout == 10
    connection.close()
    adapter.close()


def test_proxy_pools_use_policy_without_mutating_other_http_clients():
    from urllib3 import PoolManager
    from lib.llm._sync_http import model_http_adapter

    ordinary = PoolManager()
    ordinary_class = ordinary.pool_classes_by_scheme['https']
    adapter = model_http_adapter(write_timeout=60)
    proxy = adapter.proxy_manager_for('http://proxy.example:8080')
    assert proxy.pool_classes_by_scheme['https'] is adapter.poolmanager.pool_classes_by_scheme['https']
    assert ordinary.pool_classes_by_scheme['https'] is ordinary_class
    assert ordinary_class is not proxy.pool_classes_by_scheme['https']
    adapter.close()
    ordinary.clear()


def test_socks_proxy_retains_its_handshake_and_dns_transport():
    pytest.importorskip('socks')
    from urllib3.contrib.socks import SOCKSHTTPSConnection
    from lib.llm._sync_http import model_http_adapter

    adapter = model_http_adapter(write_timeout=60)
    manager = adapter.proxy_manager_for('socks5h://proxy.example:1080')
    connection_class = manager.pool_classes_by_scheme['https'].ConnectionCls
    assert issubclass(connection_class, SOCKSHTTPSConnection)
    assert manager.connection_pool_kw['_socks_options']['rdns'] is True
    assert adapter.proxy_manager_for('socks5h://proxy.example:1080').pool_classes_by_scheme['https'].ConnectionCls is connection_class
    adapter.close()


def test_sync_stream_reports_upload_failure_without_transport_retry(monkeypatch):
    import lib.llm.stream as stream
    from lib.desktop import egress

    monkeypatch.setattr(egress, 'route_request', lambda *a, **k: 'direct')
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise wrapped_write_timeout()

    monkeypatch.setattr(stream, 'get_sync_session', lambda: Mock(post=post))
    with pytest.raises(RequestUploadTimeoutError) as caught:
        stream.stream_chat(BODY, base_url='https://model.example/v1', api_key='test-key')
    assert caught.value.failure_stage == 'upload'
    assert calls == [1]


def test_async_stream_reports_upload_failure_without_transport_retry(monkeypatch):
    import lib.llm.astream as stream
    from lib.desktop import egress

    monkeypatch.setattr(egress, 'route_request', lambda *a, **k: 'direct')
    calls = []

    @contextlib.asynccontextmanager
    async def open_stream(*args, **kwargs):
        calls.append(1)
        raise httpx.WriteTimeout('upload stalled', request=httpx.Request('POST', URL, content=b'abc'))
        yield  # context-manager protocol, unreachable

    monkeypatch.setattr(stream, 'get_async_client', lambda *a, **k: Mock(stream=open_stream))
    with pytest.raises(RequestUploadTimeoutError) as caught:
        asyncio.run(stream.async_stream_chat(BODY, base_url='https://model.example/v1', api_key='test-key'))
    assert caught.value.failure_stage == 'upload'
    assert calls == [1]

"""Connection setup evidence survives requests/urllib3 proxy timeout wrapping.

Use the real model HTTP adapter and synthetic connection failures, never a live
proxy/model. A proxy TCP connection alone does not mean CONNECT has completed.
"""

from unittest.mock import Mock

import httpx
import pytest
import requests
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, ReadTimeoutError

from lib.llm._sync_http import model_http_adapter
from lib.llm._transport import classify_request_transport_error
from lib.llm.llm_errors import (
    EndpointUnreachableError, RequestUploadTimeoutError, ResponseReadTimeoutError,
)
from lib.subscription_routes import is_safe_connect_failure

pytestmark = pytest.mark.unit
URL = 'https://model.example/v1/responses'


@pytest.mark.parametrize('proxy_tcp_connected', [False, True])
@pytest.mark.parametrize('timeout_kind', ['socket', 'urllib3_read', 'proxy_tls_write'])
def test_proxy_setup_timeout_is_connect_failure_without_sending_body(
        monkeypatch, proxy_tcp_connected, timeout_kind):
    adapter = model_http_adapter(write_timeout=60, max_retries=0)
    request = requests.Request('POST', URL, data=b'model request').prepare()
    sent = Mock(side_effect=AssertionError('model request must not be sent'))
    monkeypatch.setattr(HTTPSConnection, 'send', sent)

    def connect(connection):
        # urllib3 sets this flag BEFORE waiting for the CONNECT status line.
        connection._has_connected_to_proxy = proxy_tcp_connected
        if timeout_kind == 'urllib3_read':
            raise ReadTimeoutError(None, '/', 'synthetic proxy secret')
        if timeout_kind == 'proxy_tls_write':
            raise TimeoutError('The write operation timed out')
        raise TimeoutError('synthetic proxy secret')

    monkeypatch.setattr(HTTPSConnection, 'connect', connect)
    try:
        # urllib3 may wrap setup failure as ProxyError before proxy TCP is
        # connected; either representation must retain the connect evidence.
        with pytest.raises(requests.exceptions.ConnectionError) as caught:
            adapter.send(request, timeout=(10, None), proxies={
                'https': 'http://proxy.invalid:8080'})
        assert is_safe_connect_failure(caught.value)
        error = classify_request_transport_error(caught.value, base_url=URL)
        assert isinstance(error, EndpointUnreachableError)
        assert 'synthetic proxy secret' not in str(error)
        sent.assert_not_called()
    finally:
        adapter.close()


def test_actual_response_read_timeout_keeps_read_retry_policy(monkeypatch):
    adapter = model_http_adapter(write_timeout=60, max_retries=0)
    request = requests.Request('POST', URL, data=b'model request').prepare()

    socket = Mock()
    socket.gettimeout.return_value = 10
    socket.makefile.side_effect = TimeoutError('synthetic response read timeout')
    connects = []

    def connect(connection):
        connects.append(connection)
        connection.sock = socket
        connection.is_verified = True

    # Traverse the real pool/request wrappers: connect succeeds, the body is
    # sent, and response-header reading fails. No network socket is opened.
    monkeypatch.setattr(HTTPSConnection, 'connect', connect)
    try:
        with pytest.raises(requests.exceptions.ReadTimeout) as caught:
            adapter.send(request, timeout=(10, None))
        assert not is_safe_connect_failure(caught.value)
        error = classify_request_transport_error(caught.value, base_url=URL)
        assert isinstance(error, ResponseReadTimeoutError)
        assert error.failure_stage == 'read'

        assert len(connects) == 1
        assert b'model request' in b''.join(
            call.args[0] for call in socket.sendall.call_args_list)
    finally:
        adapter.close()


def test_typed_write_failure_wins_over_old_connect_context():
    source = httpx.WriteTimeout('upload stalled', request=httpx.Request(
        'POST', URL, content=b'model request'))
    source.__context__ = ConnectTimeoutError('previous setup failure')
    error = classify_request_transport_error(source, base_url=URL, write_timeout=60)
    assert isinstance(error, RequestUploadTimeoutError)
    assert error.request_bytes == len(b'model request')

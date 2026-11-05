"""Every received response is closed, including metadata and control failures."""
from importlib import import_module
from types import SimpleNamespace

import pytest
import requests

from tofu_search.config import SearchConfig, bind_call_config

http = import_module('tofu_search.fetch.http')
pytestmark = pytest.mark.unit
URL = 'https://response.example/evidence'


class StopStream(BaseException):
    pass


class Response:
    ok = True
    status_code = 200

    def __init__(self, failure=None, length='3', close_failure=None):
        self.headers = {'Content-Type': 'text/plain', 'Content-Length': length}
        self.failure = failure
        self.close_failure = close_failure
        self.reads = 0
        self.closes = 0

    def iter_content(self, size):
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        yield b'yes'

    def close(self):
        self.closes += 1
        if self.close_failure is not None:
            raise self.close_failure


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    with bind_call_config(SearchConfig(proxy_url='', proxy_dual_attempt=False,
                                      block_private_addresses=False, fetch_max_bytes=64)):
        yield


def transport(monkeypatch, response, decode_retry):
    received = [response]
    if decode_retry:
        received.insert(0, Response(requests.exceptions.ContentDecodingError('broken br stream')))
    pending = iter(received)
    monkeypatch.setattr(http, '_session', SimpleNamespace(get=lambda *args, **kwargs: next(pending)))
    return received


@pytest.mark.parametrize('decode_retry', [False, True])
def test_malformed_length_closes_response_without_changing_validation_error(monkeypatch, decode_retry):
    response = Response(length='invalid-length')
    received = transport(monkeypatch, response, decode_retry)
    with pytest.raises(ValueError, match='invalid-length'):
        http.do_request(URL, 5)
    assert response.reads == 0
    assert all(item.closes == 1 for item in received)


@pytest.mark.parametrize('decode_retry', [False, True])
def test_declared_oversize_rejects_each_response_before_body_read(monkeypatch, decode_retry):
    response = Response(length='1000')
    received = transport(monkeypatch, response, decode_retry)
    with pytest.raises(http.HttpError) as error:
        http.do_request(URL, 5)
    assert error.value.status_code == 413
    assert response.reads == 0
    assert all(item.closes == 1 for item in received)


@pytest.mark.parametrize('decode_retry', [False, True])
def test_stream_interruption_closes_response_and_propagates_same_signal(monkeypatch, decode_retry):
    signal = StopStream('stop reading')
    received = transport(monkeypatch, Response(signal), decode_retry)
    with pytest.raises(StopStream) as error:
        http.do_request(URL, 5)
    assert error.value is signal
    assert all(item.closes == 1 for item in received)


@pytest.mark.parametrize('decode_retry', [False, True])
@pytest.mark.parametrize('failure_class', [requests.ConnectionError, StopStream])
def test_cleanup_failure_does_not_replace_stream_failure(monkeypatch, decode_retry, failure_class):
    original = failure_class('original stream failure')
    received = transport(monkeypatch, Response(original, close_failure=RuntimeError('close failed')),
                         decode_retry)
    with pytest.raises(failure_class) as error:
        http.do_request(URL, 5)
    assert error.value is original
    assert all(item.closes == 1 for item in received)


@pytest.mark.parametrize('decode_retry', [False, True])
def test_successful_read_does_not_hide_cleanup_failure(monkeypatch, decode_retry):
    original = RuntimeError('close failed')
    received = transport(monkeypatch, Response(close_failure=original), decode_retry)
    with pytest.raises(RuntimeError) as error:
        http.do_request(URL, 5)
    assert error.value is original
    assert all(item.closes == 1 for item in received)


@pytest.mark.parametrize('decode_retry', [False, True])
@pytest.mark.parametrize('read_is_control', [False, True])
def test_cleanup_interruption_preserves_the_first_control_signal(
        monkeypatch, decode_retry, read_is_control):
    original = StopStream('read interrupted') if read_is_control else requests.ConnectionError('read failed')
    cleanup = StopStream('close interrupted')
    received = transport(monkeypatch, Response(original, close_failure=cleanup), decode_retry)
    with pytest.raises(StopStream) as error:
        http.do_request(URL, 5)
    assert error.value is (original if read_is_control else cleanup)
    assert all(item.closes == 1 for item in received)


@pytest.mark.parametrize('decode_retry', [False, True])
def test_failed_consumer_releases_partial_payload_even_when_error_is_retained(monkeypatch, decode_retry):
    import gc
    import weakref

    class Payload(bytearray):
        pass

    retained = []

    def payload():
        block = Payload(b'x' * 16)
        retained.append(weakref.ref(block))
        return block

    def interrupted(size):
        for _ in range(3):
            yield payload()
        raise requests.ConnectionError('body interrupted')

    response = Response(length='0')
    response.iter_content = interrupted
    received = transport(monkeypatch, response, decode_retry)
    with pytest.raises(requests.ConnectionError) as error:
        http.do_request(URL, 5)
    assert str(error.value) == 'body interrupted'  # Keep the receipt/traceback live.
    gc.collect()
    assert len(retained) == 3
    assert all(reference() is None for reference in retained)
    assert all(item.closes == 1 for item in received)

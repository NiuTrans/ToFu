"""Anonymous GET recovery shares one budget across urllib3 retry attempts."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
import urllib3.connectionpool
import urllib3.util.retry

from tofu_search.config import SearchConfig, bind_call_config
from tofu_search.fetch import http as fetch_http

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def direct_test_transport(monkeypatch):
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy',
                'https_proxy', 'all_proxy', 'TOFU_SEARCH_PROXY_URL'):
        monkeypatch.delenv(key, raising=False)
    with bind_call_config(SearchConfig(proxy_url='', proxy_dual_attempt=False,
                                      block_private_addresses=False)):
        yield


@contextmanager
def origin(respond):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            respond(self, len(calls))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port) + '/page', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def reply(handler, status=200, headers=None):
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header('Content-Length', '7')
    handler.end_headers()
    handler.wfile.write(b'content')


def test_expired_budget_sends_no_request():
    with origin(lambda h, n: reply(h)) as (url, calls):
        with pytest.raises(requests.Timeout):
            fetch_http.do_request(url, 1, deadline_ts=time.time() - 1)
        assert calls == []


def test_transient_header_timeout_recovers_once_on_the_same_route():
    def respond(handler, count):
        if count == 1:
            time.sleep(0.2)
            return
        reply(handler)

    with origin(respond) as (url, calls):
        response, body = fetch_http.do_request(url, 0.08, deadline_ts=time.time() + 1)
        assert response.status_code == 200 and body == b'content'
        assert calls == ['/page', '/page']


def test_repeated_header_timeouts_have_one_recovery_not_a_retry_loop():
    with origin(lambda h, n: time.sleep(0.2)) as (url, calls):
        with pytest.raises(requests.RequestException):
            fetch_http.do_request(url, 0.06, deadline_ts=time.time() + 1)
        assert calls == ['/page', '/page']


def test_deadline_stops_retry_even_when_phase_timeout_has_time_left():
    with origin(lambda h, n: time.sleep(0.2)) as (url, calls):
        start = time.monotonic()
        with pytest.raises(requests.Timeout):
            fetch_http.do_request(url, 1, deadline_ts=time.time() + 0.05)
        assert time.monotonic() - start < 0.5
        assert calls == ['/page']


def test_retry_after_outside_budget_returns_the_origin_status_without_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(urllib3.util.retry.time, 'sleep', sleeps.append)
    with origin(lambda h, n: reply(h, 503, {'Retry-After': '3600'})) as (url, calls):
        with pytest.raises(fetch_http.HttpError) as error:
            fetch_http.do_request(url, 1, deadline_ts=time.time() + 2)
        assert error.value.status_code == 503
        assert sleeps == []
        assert calls == ['/page']


def test_non_retryable_http_status_remains_terminal():
    with origin(lambda h, n: reply(h, 403)) as (url, calls):
        with pytest.raises(fetch_http.HttpError) as error:
            fetch_http.do_request(url, 1, deadline_ts=time.time() + 1)
        assert error.value.status_code == 403
        assert calls == ['/page']


def test_success_does_not_add_an_extra_attempt():
    with origin(lambda h, n: reply(h)) as (url, calls):
        _, body = fetch_http.do_request(url, 1, deadline_ts=time.time() + 1)
        assert body == b'content'
        assert calls == ['/page']


def test_affordable_retry_after_is_honored_and_can_recover(monkeypatch):
    sleeps = []
    monkeypatch.setattr(urllib3.util.retry.time, 'sleep', sleeps.append)
    def respond(handler, count):
        reply(handler, 503 if count == 1 else 200, {'Retry-After': '1'})
    with origin(respond) as (url, calls):
        _, body = fetch_http.do_request(url, 1, deadline_ts=time.time() + 2)
        assert body == b'content'
        assert calls == ['/page', '/page']
        assert sleeps == [1]


def test_budget_and_read_retry_do_not_leak_into_an_unscoped_session_call():
    with origin(lambda h, n: time.sleep(0.2)) as (url, calls):
        with pytest.raises(requests.Timeout):
            fetch_http.do_request(url, 1, deadline_ts=time.time() - 1)
        with pytest.raises(requests.RequestException):
            fetch_http._session.get(url, timeout=0.05)
        assert calls == ['/page']


def test_concurrent_requests_do_not_inherit_each_others_deadline():
    def respond(handler, count):
        time.sleep(0.2)
        try:
            reply(handler)
        except BrokenPipeError:
            pass  # the short-budget caller has deliberately closed its socket
    with origin(respond) as (url, calls), ThreadPoolExecutor(max_workers=2) as workers:
        short = workers.submit(copy_context().run, fetch_http.do_request,
                               url, 1, deadline_ts=time.time() + 0.05)
        long = workers.submit(copy_context().run, fetch_http.do_request,
                              url, 1, deadline_ts=time.time() + 1)
        with pytest.raises(requests.Timeout):
            short.result(timeout=2)
        assert long.result(timeout=2)[1] == b'content'
        assert calls == ['/page', '/page']


def delay_after_response_headers(monkeypatch):
    """Pause the caller after receiving headers, before deciding on another hop."""
    received = []
    original = urllib3.connectionpool.HTTPConnectionPool._make_request

    def delayed(*args, **kwargs):
        response = original(*args, **kwargs)
        received.append(response)
        time.sleep(0.08)
        return response

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, '_make_request', delayed)
    return received


def test_budget_expiring_after_error_headers_keeps_status_and_closes_response(monkeypatch):
    received = delay_after_response_headers(monkeypatch)
    with origin(lambda h, n: reply(h, 503)) as (url, calls):
        with pytest.raises(fetch_http.HttpError) as error:
            fetch_http.do_request(url, 1, deadline_ts=time.time() + 0.05)
        assert error.value.status_code == 503
        assert calls == ['/page']
        assert received[0].closed


def test_redirect_cannot_restart_an_expired_request_budget(monkeypatch):
    received = delay_after_response_headers(monkeypatch)
    with origin(lambda h, n: reply(h, 302, {'Location': '/next'})) as (url, calls):
        with pytest.raises(requests.Timeout):
            fetch_http.do_request(url, 1, deadline_ts=time.time() + 0.05)
        assert calls == ['/page']
        assert received[0].closed


def test_incomplete_body_is_not_retried_or_returned_as_success():
    def truncated(handler, count):
        handler.send_response(200)
        handler.send_header('Content-Length', '100')
        handler.end_headers()
        handler.wfile.write(b'incomplete')

    with origin(truncated) as (url, calls):
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            fetch_http.do_request(url, 1, deadline_ts=time.time() + 1)
        assert calls == ['/page']

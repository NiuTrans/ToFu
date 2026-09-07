"""Subscription-OAuth transient 404 absorption (2026-09-02 incident).

chatgpt.com's codex backend flapped per-request HTTP 404s for minutes while
the endpoint, token, and payload were all healthy (verified live: identical
requests returned 200 minutes later). The transport classified the first
spike as RequestScopedError and killed the turn instantly. The fix absorbs
a bounded number of 404s on subscription-OAuth routes through the existing
same-route retry loop; keyed gateways keep the deterministic 404
classification, and a persistent 404 still surfaces as RequestScopedError
once the absorption budget is spent.

Run:  pytest tests/test_subscription_404_retry.py -m unit
"""
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm._transport import reset_pools_for_test  # noqa: E402
from lib.llm.stream import stream_chat  # noqa: E402

pytestmark = pytest.mark.unit

_SSE_BODY = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: [DONE]\n\n'
)
_404_BODY = b'{"detail":"Not Found"}'


class _FlappingHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'  # enable keep-alive

    requests = 0
    fail_first = 0          # how many leading requests get a 404
    _lock = threading.Lock()

    def log_message(self, *a, **kw):
        pass  # silence

    def do_POST(self):
        with _FlappingHandler._lock:
            _FlappingHandler.requests += 1
            ordinal = _FlappingHandler.requests
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length:
            self.rfile.read(length)
        if ordinal <= _FlappingHandler.fail_first:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(_404_BODY)))
            self.end_headers()
            self.wfile.write(_404_BODY)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(_SSE_BODY)))
        self.end_headers()
        self.wfile.write(_SSE_BODY)

    @classmethod
    def reset(cls, fail_first=0):
        with cls._lock:
            cls.requests = 0
            cls.fail_first = fail_first


def _start_server():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(('127.0.0.1', port), _FlappingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{port}'


@pytest.fixture(autouse=True)
def _harness(monkeypatch):
    reset_pools_for_test()
    _FlappingHandler.reset()
    # The retry loop's exponential backoff is irrelevant to these pins.
    monkeypatch.setattr('lib.llm.stream.abortable_sleep', lambda *_a, **_k: None)
    # No real subscription credential exists in tests; the OAuth token swap
    # is orthogonal to the status-code absorption under test.
    monkeypatch.setattr(
        'lib.oauth.outbound.resolve_oauth_request',
        lambda _oauth, body, headers, user_id=None: ('test', headers, body))
    yield
    reset_pools_for_test()


def _body():
    return {'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]}


class TestActivation:
    def test_absorbs_bounded_404s_on_subscription_route(self):
        from lib.llm._sse_core import (
            SUBSCRIPTION_404_MAX_RETRIES,
            activate_subscription_transient_retry,
        )
        body = _body()
        for expected in range(1, SUBSCRIPTION_404_MAX_RETRIES + 1):
            assert activate_subscription_transient_retry(
                404, oauth='codex', canonical_body=body) is True
            assert body['_subscription_404_absorbed'] == expected
        assert activate_subscription_transient_retry(
            404, oauth='codex', canonical_body=body) is False

    def test_keyed_gateway_404_never_absorbed(self):
        from lib.llm._sse_core import activate_subscription_transient_retry
        body = _body()
        assert activate_subscription_transient_retry(
            404, oauth='', canonical_body=body) is False
        assert '_subscription_404_absorbed' not in body

    def test_non_404_never_absorbed(self):
        from lib.llm._sse_core import activate_subscription_transient_retry
        for status in (400, 401, 403, 422, 500):
            assert activate_subscription_transient_retry(
                status, oauth='codex', canonical_body=_body()) is False


class TestStreamShell:
    def test_transient_flap_absorbed_then_stream_succeeds(self):
        from lib.llm._sse_core import SUBSCRIPTION_404_MAX_RETRIES
        server, base = _start_server()
        _FlappingHandler.reset(fail_first=SUBSCRIPTION_404_MAX_RETRIES)
        try:
            body = _body()
            msg, finish, _usage = stream_chat(
                body, api_key='test', base_url=base, oauth='codex',
                owner_user_id=1)
            assert finish == 'stop'
            assert msg['content'] == 'hi'
            assert (_FlappingHandler.requests
                    == SUBSCRIPTION_404_MAX_RETRIES + 1)
            assert (body['_subscription_404_absorbed']
                    == SUBSCRIPTION_404_MAX_RETRIES)
        finally:
            server.shutdown()

    def test_persistent_404_surfaces_request_scoped_after_budget(self):
        from lib.llm._sse_core import SUBSCRIPTION_404_MAX_RETRIES
        from lib.llm_errors import RequestScopedError
        server, base = _start_server()
        _FlappingHandler.reset(fail_first=99)
        try:
            with pytest.raises(RequestScopedError) as ei:
                stream_chat(_body(), api_key='test', base_url=base,
                            oauth='codex', owner_user_id=1)
            assert ei.value.status_code == 404
            assert (_FlappingHandler.requests
                    == SUBSCRIPTION_404_MAX_RETRIES + 1)
        finally:
            server.shutdown()

    def test_keyed_gateway_404_surfaces_immediately(self):
        from lib.llm_errors import RequestScopedError
        server, base = _start_server()
        _FlappingHandler.reset(fail_first=99)
        try:
            with pytest.raises(RequestScopedError):
                stream_chat(_body(), api_key='test', base_url=base)
            assert _FlappingHandler.requests == 1
        finally:
            server.shutdown()

"""Response-header arrival is recorded on every streamed direct attempt.

2026-11-08: a gateway held a request's status line for 2,000 s, and agent
rounds could not tell header buffering from model think time because no
attempt recorded when its response headers arrived. A real local HTTP server
holds the status line for a known time, then streams after a further pause:
the attempt's usage carries ``_response_headers_ms`` (request send to
headers, excluding route admission) covering the hold but not the body, on
the sync and async transports, for complete and prematurely closed streams.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from lib.llm import _transport
from lib.llm.stream_result import ProviderStreamState

pytestmark = pytest.mark.unit

HEADER_HOLD_S = 0.3
BODY_DELAY_S = 1.0
_BODY = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}


def _frame(payload):
    return b'data: ' + json.dumps(payload).encode() + b'\n\n'


def _chunk(handler, data):
    handler.wfile.write(b'%X\r\n' % len(data) + data + b'\r\n')
    handler.wfile.flush()


@contextmanager
def _held_headers_server(*, complete):
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0) or 0)
            if length:
                self.rfile.read(length)
            if release.wait(HEADER_HOLD_S):
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.flush()
            try:
                if release.wait(BODY_DELAY_S):
                    return
                _chunk(self, _frame({'choices': [{'delta': {'content': 'ok'}}]}))
                if complete:
                    _chunk(self, _frame(
                        {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}))
                    _chunk(self, b'data: [DONE]\n\n')
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)


@pytest.fixture(autouse=True)
def _direct_route(monkeypatch):
    import lib.desktop.egress as egress

    monkeypatch.setattr(egress, 'route_request', lambda url, **kw: 'direct')
    monkeypatch.setattr(_transport, 'IDLE_STREAM_TIMEOUT_S', 0)
    _transport.reset_pools_for_test()
    yield
    _transport.reset_pools_for_test()


def _assert_headers_evidence(usage):
    headers_ms = usage['_response_headers_ms']
    assert isinstance(headers_ms, int)
    # The server held the status line this long after receiving the body.
    assert headers_ms >= HEADER_HOLD_S * 1000
    # The body arrived at least BODY_DELAY_S after the headers: the key is
    # header arrival, not stream completion.
    assert usage['stream_elapsed_ms'] - headers_ms >= BODY_DELAY_S * 1000 / 2


@pytest.mark.parametrize('complete, state', [
    (True, ProviderStreamState.PROVIDER_FINISHED),
    (False, ProviderStreamState.PREMATURE_CLOSE),
])
def test_sync_attempt_usage_records_response_header_arrival(complete, state):
    from lib.llm.stream import _stream_chat_once

    with _held_headers_server(complete=complete) as base:
        result = _stream_chat_once(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]')

    assert result.state is state
    _assert_headers_evidence(result.usage)


@pytest.mark.parametrize('complete, state', [
    (True, ProviderStreamState.PROVIDER_FINISHED),
    (False, ProviderStreamState.PREMATURE_CLOSE),
])
def test_async_attempt_usage_records_response_header_arrival(complete, state):
    from lib.llm.astream import _async_stream_chat_once

    async def run():
        with _held_headers_server(complete=complete) as base:
            return await _async_stream_chat_once(
                dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]')

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(asyncio.wait_for(run(), 30))
    finally:
        loop.close()

    assert result.state is state
    _assert_headers_evidence(result.usage)

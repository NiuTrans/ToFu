"""Silent-stream deadline: headers then silence must never hang a turn.

2026-10-28 production: a provider sent response headers and then nothing; a
root-agent round sat in ``ssl.read`` for hours because the rolling idle window
is sealed off by default and interactive rounds pass no attempt or first-byte
deadline. A real local HTTP server reproduces it: the call must end with the
typed retryable ``ResponseReadTimeoutError(cause_type='stream_silence')``
within the deadline plus slack, while a slow but steady stream completes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from lib.llm import _transport
from lib.llm.llm_errors import AbortedError, ResponseReadTimeoutError
from lib.llm.stream_result import ProviderStreamState

pytestmark = pytest.mark.unit

DEADLINE_S = 0.6
SLACK_S = 1.5


def _frame(payload):
    return b'data: ' + json.dumps(payload).encode() + b'\n\n'


@contextmanager
def _server(behaviour):
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0) or 0)
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.flush()
            try:
                behaviour(self, release)
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


def _chunk(handler, data):
    handler.wfile.write(b'%X\r\n' % len(data) + data + b'\r\n')
    handler.wfile.flush()


def _silent_after_headers(handler, release):
    release.wait(30)


def _slow_steady(handler, release):
    # Each gap is ~2/3 of the deadline; total stream far exceeds it.
    for piece in ('th', 'in', 'ki', 'ng'):
        _chunk(handler, _frame(
            {'choices': [{'delta': {'reasoning_content': piece}}]}))
        if release.wait(DEADLINE_S * 0.65):
            return
    _chunk(handler, b': keep-alive\n\n')
    if release.wait(DEADLINE_S * 0.65):
        return
    _chunk(handler, _frame({'choices': [{'delta': {'content': 'ok'}}]}))
    _chunk(handler, _frame(
        {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}))
    _chunk(handler, b'data: [DONE]\n\n')
    handler.wfile.write(b'0\r\n\r\n')
    handler.wfile.flush()


@pytest.fixture(autouse=True)
def _short_deadline(monkeypatch):
    import lib.desktop.egress as egress

    monkeypatch.setattr(egress, 'route_request', lambda url, **kw: 'direct')
    monkeypatch.setattr(_transport, 'IDLE_STREAM_TIMEOUT_S', 0)
    monkeypatch.setattr(_transport, 'SILENT_STREAM_DEADLINE_SECONDS', DEADLINE_S)
    monkeypatch.setattr(_transport, 'ABORT_POLL_INTERVAL', 0.05)
    _transport.reset_pools_for_test()
    yield
    _transport.reset_pools_for_test()


_BODY = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}


def _bounded(fn):
    """Run ``fn`` on a daemon thread so a regression fails instead of hanging."""
    outcome = {}

    def run():
        try:
            outcome['value'] = fn()
        except BaseException as error:  # re-raised below
            outcome['error'] = error

    started = time.monotonic()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(DEADLINE_S + SLACK_S + 5)
    assert not worker.is_alive(), 'silent stream hung the call'
    return outcome, time.monotonic() - started


def test_default_deadline_is_a_named_liveness_contract_constant(monkeypatch):
    monkeypatch.undo()
    assert _transport.SILENT_STREAM_DEADLINE_SECONDS == 900


def test_sync_headers_then_silence_raises_typed_timeout():
    from lib.llm.stream import _stream_chat_once

    with _server(_silent_after_headers) as base:
        outcome, elapsed = _bounded(lambda: _stream_chat_once(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]'))

    error = outcome.get('error')
    assert isinstance(error, ResponseReadTimeoutError), outcome
    assert error.cause_type == 'stream_silence'
    assert DEADLINE_S * 0.9 <= elapsed <= DEADLINE_S + SLACK_S


def test_sync_silence_escapes_same_slot_retry_to_dispatch():
    from lib.llm.stream import stream_chat

    with _server(_silent_after_headers) as base:
        outcome, elapsed = _bounded(lambda: stream_chat(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]'))

    error = outcome.get('error')
    assert isinstance(error, ResponseReadTimeoutError), outcome
    assert error.cause_type == 'stream_silence'
    assert elapsed <= DEADLINE_S + SLACK_S


def test_sync_slow_steady_stream_completes():
    from lib.llm.stream import _stream_chat_once

    with _server(_slow_steady) as base:
        outcome, elapsed = _bounded(lambda: _stream_chat_once(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]'))

    assert 'error' not in outcome, outcome.get('error')
    result = outcome['value']
    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message.get('content') == 'ok'
    assert elapsed > DEADLINE_S * 2


def test_sync_user_stop_during_silent_stream_aborts():
    from lib.llm.stream import _stream_chat_once

    stop_at = time.monotonic() + DEADLINE_S / 3
    with _server(_silent_after_headers) as base:
        outcome, elapsed = _bounded(lambda: _stream_chat_once(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]',
            abort_check=lambda: time.monotonic() >= stop_at))

    assert isinstance(outcome.get('error'), AbortedError), outcome
    assert elapsed < DEADLINE_S


def _run_async(coro_factory):
    def run():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            loop.close()
    return _bounded(run)


def test_async_headers_then_silence_raises_typed_timeout():
    from lib.llm.astream import async_stream_chat

    with _server(_silent_after_headers) as base:
        outcome, elapsed = _run_async(lambda: async_stream_chat(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]'))

    error = outcome.get('error')
    assert isinstance(error, ResponseReadTimeoutError), outcome
    assert error.cause_type == 'stream_silence'
    assert DEADLINE_S * 0.9 <= elapsed <= DEADLINE_S + SLACK_S


def test_async_slow_steady_stream_completes():
    from lib.llm.astream import async_stream_chat

    with _server(_slow_steady) as base:
        outcome, elapsed = _run_async(lambda: async_stream_chat(
            dict(_BODY), api_key='sk-x', base_url=base, log_prefix='[t]'))

    assert 'error' not in outcome, outcome.get('error')
    msg, finish, _usage = outcome['value']
    assert finish == 'stop'
    assert msg.get('content') == 'ok'
    assert elapsed > DEADLINE_S * 2

"""Connection-pool reuse for the LLM streaming transports.

Before pooling, ``stream.py`` did a bare ``requests.post`` and ``astream.py``
built + tore down a fresh ``httpx.AsyncClient`` per call — a full TCP/TLS
handshake on the critical path of EVERY turn. These tests prove the pooled
``get_sync_session`` / ``get_async_client`` helpers reuse one keep-alive
connection across sequential turns.

The substantive assertion is SERVER-SIDE: a keep-alive HTTP/1.1 mock server
counts how many distinct TCP connections it accepted. N sequential requests
over a shared pool ⇒ 1 connection; a fresh client per call ⇒ N connections.
"""
import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm._transport import (  # noqa: E402
    LLM_MAX_CONNECTIONS,
    LLM_MAX_KEEPALIVE_CONNECTIONS,
    get_async_client,
    get_sync_session,
    reset_pools_for_test,
)
from lib.llm.astream import async_stream_chat  # noqa: E402
from lib.llm.stream import stream_chat  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

# A minimal, complete OpenAI-style SSE body (Content-Length set → clean
# HTTP/1.1 keep-alive so the connection is returned to the pool for reuse).
_SSE_BODY = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: [DONE]\n\n'
)


class _CountingHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'  # enable keep-alive

    connections = 0          # distinct TCP connections accepted
    requests = 0             # total POSTs served
    _lock = threading.Lock()

    def setup(self):
        # setup() runs once per accepted connection.
        with _CountingHandler._lock:
            _CountingHandler.connections += 1
        super().setup()

    def log_message(self, *a, **kw):
        pass  # silence

    def do_POST(self):
        with _CountingHandler._lock:
            _CountingHandler.requests += 1
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(_SSE_BODY)))
        self.end_headers()
        self.wfile.write(_SSE_BODY)

    @classmethod
    def reset(cls):
        with cls._lock:
            cls.connections = 0
            cls.requests = 0


def _start_server():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(('127.0.0.1', port), _CountingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{port}'


def _clean():
    reset_pools_for_test()
    _CountingHandler.reset()


# ─────────────────────────── sync ───────────────────────────

def test_sync_session_is_a_singleton():
    _clean()
    try:
        assert get_sync_session() is get_sync_session()
    finally:
        _clean()


def test_idle_pool_is_bounded_for_personal_server():
    """Keep parallel work legal but retain only a small warm idle set.

    A peer-closed idle connection otherwise remains CLOSE_WAIT until the pool
    is touched again. The cap is retention policy, not a concurrency gate.
    """
    _clean()
    loop = asyncio.new_event_loop()
    try:
        session = get_sync_session()
        adapter = session.get_adapter('http://')
        assert adapter._pool_maxsize == LLM_MAX_KEEPALIVE_CONNECTIONS

        async def _probe():
            client = get_async_client(None)
            pool = client._transport._pool
            return pool._max_connections, pool._max_keepalive_connections

        active, idle = loop.run_until_complete(_probe())
        assert active == LLM_MAX_CONNECTIONS
        assert idle == LLM_MAX_KEEPALIVE_CONNECTIONS
        assert idle < active, 'idle retention accidentally became the active cap'
    finally:
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        _clean()


def test_sync_reuses_one_connection_across_turns():
    """3 sequential sync streams ⇒ the server sees 1 connection, 3 requests."""
    _clean()
    server, base = _start_server()
    try:
        for _ in range(3):
            msg, finish, _usage = stream_chat(
                {'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]},
                api_key='test', base_url=base)
            assert finish == 'stop'
            assert msg['content'] == 'hi'
        assert _CountingHandler.requests == 3
        # The whole point: keep-alive reuse, NOT a fresh connection per turn.
        assert _CountingHandler.connections == 1, (
            'expected 1 pooled connection for 3 turns, got %d'
            % _CountingHandler.connections)
    finally:
        server.shutdown()
        _clean()


# ─────────────────────────── async ───────────────────────────

def test_async_client_pooled_per_loop_and_proxy():
    _clean()
    loop = asyncio.new_event_loop()
    try:
        async def _probe():
            a = get_async_client(None)
            b = get_async_client(None)
            c = get_async_client('http://proxy:8080')
            return a, b, c

        a, b, c = loop.run_until_complete(_probe())
        assert a is b, 'same (loop, proxy) must return the same client'
        assert c is not a, 'a different proxy must get its own client'
        assert not a.is_closed
        assert a._trust_env is False, (
            'proxy=None must be truly direct, not silently reuse env proxies')
    finally:
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        _clean()


def test_async_reuses_one_connection_across_turns():
    """3 sequential async streams on one loop ⇒ 1 connection, 3 requests, client stays open."""
    _clean()
    server, base = _start_server()
    loop = asyncio.new_event_loop()
    try:
        async def _run():
            for _ in range(3):
                msg, finish, _usage = await async_stream_chat(
                    {'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]},
                    api_key='test', base_url=base)
                assert finish == 'stop'
                assert msg['content'] == 'hi'
            # Borrowed client must NOT be closed after use.
            assert not get_async_client(None).is_closed

        loop.run_until_complete(_run())
        assert _CountingHandler.requests == 3
        assert _CountingHandler.connections == 1, (
            'expected 1 pooled connection for 3 async turns, got %d'
            % _CountingHandler.connections)
    finally:
        server.shutdown()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        _clean()


if __name__ == '__main__':
    test_sync_session_is_a_singleton()
    test_sync_reuses_one_connection_across_turns()
    test_async_client_pooled_per_loop_and_proxy()
    test_async_reuses_one_connection_across_turns()
    print('All connection-reuse tests passed.')

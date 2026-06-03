#!/usr/bin/env python3
"""Unit tests for lib.http_client — sync + async paths.

Uses a tiny in-process HTTP server bound to 127.0.0.1 so the tests are
hermetic (no network required) and exercise the actual sockets +
header passing path.
"""

import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── In-process mock server ─────────────────────────────────────

_received_requests: list[dict] = []


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw): pass  # silence

    def _record(self, body=None):
        _received_requests.append({
            'method': self.command,
            'path': self.path,
            'headers': dict(self.headers),
            'body': body,
        })

    def do_GET(self):
        self._record()
        if self.path == '/echo-headers':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(dict(self.headers)).encode())
            return
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for line in (b'first\n', b'second\n', b'third\n'):
                # chunked encoding
                self.wfile.write(f'{len(line):X}\r\n'.encode())
                self.wfile.write(line)
                self.wfile.write(b'\r\n')
            self.wfile.write(b'0\r\n\r\n')
            return
        if self.path.startswith('/status/'):
            code = int(self.path.split('/')[-1])
            self.send_response(code)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            'method': 'GET',
            'path': self.path,
        }).encode())

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n) if n else b''
        self._record(body=body.decode())
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        try:
            payload = json.loads(body) if body else None
        except Exception:
            payload = body.decode(errors='replace')
        self.wfile.write(json.dumps({
            'method': 'POST',
            'path': self.path,
            'received': payload,
        }).encode())


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    """Spawn a daemon mock server and return (server, base_url)."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(('127.0.0.1', port), _MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{port}'


_server, _base = _start_server()


def _last_request():
    return _received_requests[-1]


def _clear():
    _received_requests.clear()


# ─── Sync tests ────────────────────────────────────────────────

def test_http_get_basic():
    from lib.http_client import http_get
    _clear()
    resp = http_get(f'{_base}/hello')
    assert resp.status_code == 200
    assert resp.json()['method'] == 'GET'
    assert resp.json()['path'] == '/hello'
    _ok('http_get returns Response with .json()')


def test_http_get_default_user_agent():
    from lib.http_client import http_get
    _clear()
    http_get(f'{_base}/echo-headers')
    rec = _last_request()
    ua = rec['headers'].get('User-Agent', '')
    assert ua.startswith('Tofu/'), f'expected Tofu/* UA, got {ua!r}'
    _ok('http_get sets default User-Agent: Tofu/<version>')


def test_http_get_custom_headers_override_default_ua():
    from lib.http_client import http_get
    _clear()
    http_get(f'{_base}/echo-headers',
             headers={'User-Agent': 'CustomBot/2.0', 'X-Trace': 'abc'})
    rec = _last_request()
    assert rec['headers']['User-Agent'] == 'CustomBot/2.0'
    assert rec['headers']['X-Trace'] == 'abc'
    _ok('custom User-Agent + extra headers passed through')


def test_http_get_query_params():
    from lib.http_client import http_get
    _clear()
    resp = http_get(f'{_base}/p', params={'q': 'hello world', 'n': 5})
    assert resp.status_code == 200
    rec = _last_request()
    # requests should URL-encode the query
    assert 'q=hello' in rec['path']
    assert 'n=5' in rec['path']
    _ok('http_get(params={...}) URL-encodes query string')


def test_http_post_json():
    from lib.http_client import http_post
    _clear()
    resp = http_post(f'{_base}/post', json={'a': 1, 'b': 'hi'})
    assert resp.status_code == 200
    data = resp.json()
    assert data['received'] == {'a': 1, 'b': 'hi'}
    rec = _last_request()
    assert rec['headers']['Content-Type'].startswith('application/json')
    _ok('http_post(json=) sends JSON body + Content-Type')


def test_http_post_form_data():
    from lib.http_client import http_post
    _clear()
    resp = http_post(f'{_base}/form',
                      data={'field1': 'value1', 'field2': 'value2'})
    assert resp.status_code == 200
    rec = _last_request()
    assert 'field1=value1' in rec['body']
    _ok('http_post(data=dict) sends form-encoded body')


def test_http_status_codes_passthrough():
    from lib.http_client import http_get
    for code in (404, 500, 503):
        resp = http_get(f'{_base}/status/{code}')
        assert resp.status_code == code
    _ok('http_get returns response unchanged for 4xx/5xx (no auto-raise)')


def test_http_stream_context_manager():
    from lib.http_client import http_stream
    received = []
    with http_stream('GET', f'{_base}/stream', timeout=5) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            received.append(line.decode() if line else '')
    assert 'first' in received
    assert 'second' in received
    assert 'third' in received
    _ok('http_stream() context manager + iter_lines() works')


def test_http_request_method_dispatcher():
    from lib.http_client import http_request
    _clear()
    resp = http_request('GET', f'{_base}/test')
    assert resp.status_code == 200
    resp = http_request('POST', f'{_base}/test', json={'k': 'v'})
    assert resp.status_code == 200
    _ok('http_request(method=...) dispatches both GET and POST')


def test_http_proxy_arg_passes_through():
    """When a custom proxies dict is provided, http_request uses it as-is."""
    from lib.http_client import http_get
    # Provide explicit proxies={} → bypass proxy entirely
    resp = http_get(f'{_base}/hello', proxies={})
    assert resp.status_code == 200
    _ok('http_get(proxies=...) overrides auto-applied proxy')


# ─── Async tests ───────────────────────────────────────────────

def test_async_http_get():
    from lib.http_client import async_http_get

    async def _t():
        resp = await async_http_get(f'{_base}/hello', timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data['method'] == 'GET'

    asyncio.run(_t())
    _ok('async_http_get returns httpx.Response')


def test_async_http_post_json():
    from lib.http_client import async_http_post

    async def _t():
        resp = await async_http_post(f'{_base}/post',
                                       json={'msg': 'hello'}, timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data['received'] == {'msg': 'hello'}

    asyncio.run(_t())
    _ok('async_http_post(json=) round-trips')


def test_async_http_default_user_agent():
    from lib.http_client import async_http_get

    async def _t():
        _clear()
        await async_http_get(f'{_base}/echo-headers', timeout=5)
        rec = _last_request()
        assert rec['headers'].get('User-Agent', '').startswith('Tofu/')

    asyncio.run(_t())
    _ok('async_http_get sets default User-Agent')


def test_async_http_stream():
    from lib.http_client import async_http_stream

    async def _t():
        received = []
        async with async_http_stream('GET', f'{_base}/stream',
                                       timeout=5) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line:
                    received.append(line)
        assert 'first' in received
        assert 'second' in received

    asyncio.run(_t())
    _ok('async_http_stream() async context manager + aiter_lines()')


def test_async_http_status_codes():
    from lib.http_client import async_http_get

    async def _t():
        for code in (404, 500, 503):
            resp = await async_http_get(f'{_base}/status/{code}', timeout=5)
            assert resp.status_code == code

    asyncio.run(_t())
    _ok('async_http_get passes through non-2xx responses')


def main():
    print()
    print(_color('═══ http_client.py Unit Tests ═══', '36'))
    print()
    tests = [
        test_http_get_basic,
        test_http_get_default_user_agent,
        test_http_get_custom_headers_override_default_ua,
        test_http_get_query_params,
        test_http_post_json,
        test_http_post_form_data,
        test_http_status_codes_passthrough,
        test_http_stream_context_manager,
        test_http_request_method_dispatcher,
        test_http_proxy_arg_passes_through,
        test_async_http_get,
        test_async_http_post_json,
        test_async_http_default_user_agent,
        test_async_http_stream,
        test_async_http_status_codes,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    finally_shutdown()
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


def finally_shutdown():
    try:
        _server.shutdown()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    finally:
        finally_shutdown()

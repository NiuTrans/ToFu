"""Real socket specification: small SSE frames must arrive before EOF.

No external network/model. The server withholds its terminal frame until the
consumer receives the first frame, so a fill-to-64KiB reader cannot pass.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import zlib

import pytest
import requests

from lib.llm._sse_framer import SSEFramer
from lib.llm.stream import _iter_response_bytes

pytestmark = pytest.mark.unit


@contextmanager
def _server(framing, compressed):
    release = threading.Event()
    first = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    last = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    if compressed:
        compressor = zlib.compressobj(wbits=31)
        first = compressor.compress(first) + compressor.flush(zlib.Z_SYNC_FLUSH)
        last = compressor.compress(last) + compressor.flush(zlib.Z_FINISH)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            if framing == 'length':
                self.send_header('Content-Length', str(len(first) + len(last)))
            if framing == 'chunked':
                self.send_header('Transfer-Encoding', 'chunked')
            if compressed:
                self.send_header('Content-Encoding', 'gzip')
            self.end_headers()
            try:
                for data in (first, last):
                    if data is last:
                        release.wait(4)
                    if framing == 'chunked':
                        data = f'{len(data):x}\r\n'.encode() + data + b'\r\n'
                    self.wfile.write(data)
                    self.wfile.flush()
                if framing == 'chunked':
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/stream', release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)


@pytest.mark.parametrize('framing', ['close', 'length', 'chunked'])
@pytest.mark.parametrize('compressed', [False, True])
def test_sse_first_frame_is_delivered_before_server_finishes(framing, compressed):
    received = threading.Event()
    events = []
    errors = []
    framer = SSEFramer()
    with _server(framing, compressed) as (url, release):
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, stream=True, timeout=(2, 5)) as response:
                def consume():
                    try:
                        for raw in _iter_response_bytes(response):
                            batch = framer.feed(raw)
                            events.extend(batch)
                            if batch:
                                received.set()
                        events.extend(framer.finalize())
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=consume, daemon=True)
                worker.start()
                early = received.wait(1)
                release.set()
                worker.join(3)
                # Assertions live in the main thread so a consumer failure
                # cannot be laundered through the thread's error capture.
                assert not worker.is_alive()
                for error in errors:
                    raise error
                assert framer.drain_issues().count == 0
                assert early, 'reader withheld an already-flushed SSE frame until EOF'
                assert len(events) == 3
                assert events[-1].data == '[DONE]'

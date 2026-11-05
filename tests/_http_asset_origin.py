"""Owned loopback replay of captured application response bytes and headers."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit


@contextmanager
def serve_asset_responses(responses):
    """Keep browser transport separate from actual application projections."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            response = responses.get(urlsplit(self.path).path)
            if response is None:
                self.send_error(404)
                return
            body, headers = response
            self.send_response(200)
            for name, value in headers.items():
                if name.lower() not in ('content-length', 'content-encoding', 'transfer-encoding'):
                    self.send_header(name, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, name='browser-asset-origin', daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

"""Complete public-fetch reception through a hermetic loopback HTTP server."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from lib.safe_fetch import fetch_public_file

pytestmark = pytest.mark.api


@pytest.mark.parametrize('declared', [True, False])
def test_real_http_complete_file_reception(monkeypatch, declared):
    block = bytes(range(256)) * 256
    count = 177
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'image/png; charset=binary')
            if declared:
                self.send_header('Content-Length', str(len(block) * count))
            self.end_headers()
            for _ in range(count):
                self.wfile.write(block)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    monkeypatch.setenv('TOFU_TEST_FETCH_ALLOW', 'localhost')
    monkeypatch.setattr('lib.safe_fetch.proxies_for', lambda url: {})
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)
    thread.start()
    source = None
    try:
        url = f'http://localhost:{server.server_port}/image'
        source, content_type, final_url = fetch_public_file(
            url, allow_hosts_env='TOFU_TEST_FETCH_ALLOW', enforce_total_timeout=False)
        with source:
            actual = hashlib.sha256()
            received = 0
            while chunk := source.read(65536):
                actual.update(chunk)
                received += len(chunk)
        expected = hashlib.sha256()
        for _ in range(count):
            expected.update(block)
        assert actual.digest() == expected.digest()
        assert received == len(block) * count
        assert source.closed
        assert content_type == 'image/png'
        assert final_url == url
    finally:
        if source is not None:
            source.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

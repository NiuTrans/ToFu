"""Real loopback transport refuses redirects and bounds evidence reads."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError

import pytest

from evaluations.application_api import JsonAPI

pytestmark = pytest.mark.unit


def test_loopback_transport_bypasses_ambient_proxy_and_never_follows_redirect(monkeypatch):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            if self.path == '/api/redirect':
                self.send_response(302)
                self.send_header('Location', '/api/unexpected')
                self.end_headers()
                return
            raw = json.dumps({'data': {'value': 'x' * 128}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    try:
        monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
        monkeypatch.setenv('no_proxy', '')
        api = JsonAPI(f'http://127.0.0.1:{server.server_port}', token='fixture-token')
        assert api.call('/api/value')['value'] == 'x' * 128
        with pytest.raises(HTTPError) as caught:
            api.call('/api/redirect')
        assert caught.value.code == 302
        assert [row[0] for row in seen] == ['/api/value', '/api/redirect']
        small = JsonAPI(api.origin, maximum_bytes=32)
        with pytest.raises(ValueError, match='ceiling'):
            small.call('/api/value')
        with pytest.raises(ValueError, match='relative'):
            api.call('https://example.com/api/value')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()

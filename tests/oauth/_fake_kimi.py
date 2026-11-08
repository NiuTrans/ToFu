"""Fake Kimi Code services for subscription tests.

One loopback HTTP server plays both sides Kimi exposes to a signed-in client:
the RFC 8628 device grant at ``/api/oauth/*`` (auth.kimi.com) and the
OpenAI-compatible coding endpoint at ``/coding/v1/chat/completions``
(api.kimi.com). Scripts are queues of ``(status, json_body)`` replies; every
request is recorded with its headers and decoded form/JSON body so tests can
assert the wire contract, including the honest client identity.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class FakeKimi:
    def __init__(self):
        self.requests: list[dict] = []
        self.device_replies: deque = deque()
        self.token_replies: deque = deque()
        self.chat_replies: deque = deque()
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length).decode('utf-8') if length else ''
                ctype = self.headers.get('Content-Type') or ''
                if 'application/x-www-form-urlencoded' in ctype:
                    body = {k: v[0] for k, v in parse_qs(raw).items()}
                else:
                    try:
                        body = json.loads(raw) if raw else {}
                    except ValueError:
                        body = {'_raw': raw}
                record = {'path': self.path, 'headers': dict(self.headers),
                          'body': body}
                with fake._lock:
                    fake.requests.append(record)
                    if self.path == '/api/oauth/device_authorization':
                        queue = fake.device_replies
                    elif self.path == '/api/oauth/token':
                        queue = fake.token_replies
                    elif self.path == '/coding/v1/chat/completions':
                        queue = fake.chat_replies
                    else:
                        queue = None
                    reply = queue.popleft() if queue else (404, {'error': 'not_found'})
                status, payload = reply
                data = json.dumps(payload).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name='fake-kimi')

    @property
    def origin(self) -> str:
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def start(self) -> 'FakeKimi':
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> list[str]:
        with self._lock:
            return [row['path'] for row in self.requests]

    def last(self, path: str) -> dict:
        with self._lock:
            return [row for row in self.requests if row['path'] == path][-1]

    def config(self) -> dict:
        """KIMI_OAUTH_CONFIG overrides that point every endpoint here."""
        return {
            'device_authorization_url': self.origin + '/api/oauth/device_authorization',
            'token_url': self.origin + '/api/oauth/token',
            'api_base': self.origin + '/coding/v1',
            # Keep polls fast; protocol behaviour (slow_down) still applies.
            'device_poll_interval': 1,
        }

"""Loopback protocol fixture; production Swift URLSession sends every request."""
import json
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Foundation Process can inherit a blocked signal mask from a Swift executor.
# Own the fixture lifecycle explicitly so Process.terminate() can join it.
signal.pthread_sigmask(signal.SIG_SETMASK, [])

started = threading.Event()
release = threading.Event()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, body=b'', headers=()):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers = (*headers, ('Content-Type', 'application/json'))
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/redirect':
            self.reply(302, headers=(('Location', '/echo'), ('Set-Cookie', 'one=1; Path=/'),
                ('Set-Cookie', 'two=2; Expires=Wed, 28 Oct 2037 00:00:00 GMT; Path=/')))
        elif path == '/slow':
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.wfile.write(b'1\r\nx\r\n'); self.wfile.flush()
            started.set()
            release.wait(10)
            try:
                self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == '/state':
            self.reply(200, {'started': started.is_set()})
        elif path == '/release':
            release.set()
            self.reply(200, {})
        elif path in ('/oversized', '/chunked'):
            self.send_response(200)
            self.send_header('Content-Length' if path == '/oversized' else 'Transfer-Encoding',
                             '1048576' if path == '/oversized' else 'chunked')
            self.end_headers()
            try:
                for _ in range(1024):
                    self.wfile.write(b'x' * 1024 if path == '/oversized' else b'400\r\n' + b'x' * 1024 + b'\r\n')
                    self.wfile.flush()
                if path == '/chunked': self.wfile.write(b'0\r\n\r\n')
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == '/app/':
            self.reply(302, headers=(('Location', '/login?to=%2Fapp%2F'),))
        elif path == '/login':
            self.reply(200, "<form class='login-form' method='post'><input type='hidden' name='base' value='.'><input type='hidden' name='csrf' value='fixture-token'><input type='password' name='password'></form>")
        elif path == '/app/api/v4/meta':
            self.reply(200, {'data': {'apiMajor': 4}})
        elif path == '/app/api/v4/conversations':
            if 'code-server-session=fixture' in self.headers.get('Cookie', ''):
                self.reply(200, {'requestId': 'loopback', 'data': {'items': [], 'page': {}}, 'meta': {'serverTimeMs': 1}})
            else:
                self.reply(302, headers=(('Location', '/login'),))
        else:
            self.reply(200, {'path': self.path, 'cookie': self.headers.get('Cookie', '')})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()
        fields = parse_qs(raw, keep_blank_values=True)
        if urlsplit(self.path).path == '/login':
            if fields == {'base': ['.'], 'csrf': ['fixture-token'], 'password': ['local-test-password']}:
                self.reply(302, headers=(('Location', '/app/'), ('Set-Cookie', 'code-server-session=fixture; Path=/; HttpOnly')))
            else:
                self.reply(400, {'error': 'form mismatch'})
        else:
            self.reply(200, {'fields': fields, 'contentType': self.headers.get('Content-Type', '')})


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print(f'http://127.0.0.1:{server.server_port}', flush=True)
server.serve_forever()

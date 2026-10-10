"""Real Chromium layout fixture served locally without booting storage.

Owns an ephemeral HTTP origin and page. Loads the shipped stylesheet; tests
load the actual frontend owner graphs. API/application journeys remain on
live_server and must not substitute this fixture for backend validation.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def frontend_visual_origin():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == '/':
                body = b'<!doctype html><html><head><link rel="stylesheet" href="/static/styles.css"></head><body></body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def frontend_visual_page(browser, frontend_visual_origin):
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(frontend_visual_origin, wait_until='load')
    try:
        yield page
        assert not errors, f'Uncaught frontend browser errors: {errors}'
    finally:
        page.close()

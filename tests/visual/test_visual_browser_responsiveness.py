"""Recovered main-thread stalls travel through the real browser relay seam."""
import json
import pytest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse
from scripts.frontend_bundle_canary import _api_body, _entry_file, _shell

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


def test_recovered_stall_reaches_client_log_relay(browser):
    # Serve the production bundle on loopback with the same in-memory API
    # contract as the publication canary; storage is outside this browser test.
    root = Path(__file__).resolve().parents[2]
    artifact_dir = root / 'static/vite'
    shell = _shell(_entry_file(artifact_dir, artifact_dir / 'manifest.json'))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            if self.path == '/':
                body = shell.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context(service_workers='block')
    page = context.new_page()
    batches = []

    def capture(route):
        path = urlparse(route.request.url).path
        if path.endswith('/logs/client'):
            batches.extend(json.loads(route.request.post_data)['entries'])
        route.fulfill(status=200, content_type='application/json', body=json.dumps(_api_body(path)))

    try:
        page.route('**/api/**', capture)
        page.add_init_script("window.__stallAppReady = false; window.addEventListener('tofu:app-ready', () => {window.__stallAppReady = true;});")
        page.goto(f'http://127.0.0.1:{server.server_port}/', wait_until='domcontentloaded')
        page.wait_for_function('window.__stallAppReady === true')
        page.wait_for_function("window.TofuModules && window.TofuModules.version === 3")
        # Let startup long-task checkpoints expire before measuring this stall.
        page.wait_for_timeout(5500)
        # Observe native Chromium longtask/long-animation-frame delivery, rather
        # than injecting a diagnostic event or relying on a timer approximation.
        batches.clear()
        started = page.evaluate("""() => new Promise(resolve => setTimeout(() => {
            const start = performance.now(); const until = start + 450;
            while (performance.now() < until) { /* intentional main-thread stall */ }
            resolve(start);
        }, 0))""")
        from time import monotonic
        deadline = monotonic() + 10
        records = []
        while monotonic() < deadline:
            page.wait_for_timeout(100)
            records = [entry for entry in batches
                       if 'browser.main_thread_stall' in entry.get('msg', '')]
            records = [entry for entry in records
                       if json.loads(entry['msg'].split('] ', 1)[1])['startMs'] >= started - 10]
            if records:
                break
        assert records, 'Recovered native browser stall did not reach the existing log relay'
        evidence = json.loads(records[0]['msg'].split('] ', 1)[1])
        assert evidence['durationMs'] >= 400
        assert evidence['kind'] in ('longtask', 'long-animation-frame')
        assert 'conversationId' in evidence
        assert records[0]['lv'] == 'warn'
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

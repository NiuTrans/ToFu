"""Opt-in real Chromium proxy evidence; servers are owned loopback fixtures."""

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('TOFU_SEARCH_TEST_BROWSER') != '1',
                              reason='opt-in real Playwright runtime')


def test_browser_recovers_dynamic_source_through_configured_and_authenticated_proxy(tmp_path):
    from tofu_search.config import SearchConfig, bind_call_config
    from tofu_search.fetch.playwright_pool import PlaywrightPool

    fact = 'The eastern observatory opened on 17 May 1986.'
    # The proxy serves this reserved target itself: no upstream socket or
    # DNS is used. Direct access to .invalid cannot produce the fixture fact.
    target = 'http://browser-proxy-test.invalid/archive'
    authorized = 'Basic ' + base64.b64encode(b'reader@team:p:ss').decode()
    rows = []

    class Handler(BaseHTTPRequestHandler):
        require_auth = False

        def do_GET(self):
            valid_auth = self.headers.get('Proxy-Authorization') == authorized
            rows.append({'path': self.path, 'authorized': valid_auth,
                         'cookie_present': 'Cookie' in self.headers})
            if self.require_auth and not valid_auth:
                self.send_response(407)
                self.send_header('Proxy-Authenticate', 'Basic realm="owned-fixture"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            path = urlsplit(self.path).path
            if path == '/unanswered':
                self.close_connection = True
                return
            status = 403 if path in ('/error', '/recover') else 200
            if path == '/error':
                body = '<main>' + fact * 20 + '</main>'
            elif path in ('/recover', '/fail-later', '/fail-network'):
                destination = {'/recover': '/archive', '/fail-later': '/error',
                               '/fail-network': '/unanswered'}[path]
                body = '<main>' + fact + '</main><script>setTimeout(()=>location.replace('
                body += json.dumps(destination) + '),150);</script>'
            else:
                body = ('<div id="app"><main></main></div><script>'
                        'setTimeout(()=>{document.querySelector("main").innerHTML='
                        + json.dumps('<h1>Archive</h1><p>' + fact + '</p>')
                        + '},150);</script>')
                if path == '/resources':
                    body += '<iframe src="/error"></iframe><script>fetch("/error")</script>'
            body = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    pool = PlaywrightPool()
    results = []
    try:
        for credentials in ('', 'reader%40team:p%3Ass@'):
            Handler.require_auth = bool(credentials)
            proxy = f'http://{credentials}127.0.0.1:{server.server_port}'
            with bind_call_config(SearchConfig(proxy_url=proxy, proxy_dual_attempt=False)):
                text = pool.fetch(target, timeout=5, max_chars=0)
            results.append({'proxy_auth_required': bool(credentials), 'text': text})
            assert text and fact in text
            assert 'setTimeout' not in text
        Handler.require_auth = False
        proxy = f'http://127.0.0.1:{server.server_port}'
        for path in ('/error', '/recover', '/fail-later', '/resources', '/fail-network'):
            with bind_call_config(SearchConfig(proxy_url=proxy, proxy_dual_attempt=False)):
                text = pool.fetch(target.replace('/archive', path), timeout=5, max_chars=0)
            results.append({'path': path, 'text': text})
            if path in ('/error', '/fail-later', '/fail-network'):
                assert text is None
            else:
                assert text and fact in text
        assert any(row['path'].endswith('/unanswered') for row in rows)
        assert any(row['path'] == target and row['authorized'] for row in rows)
        assert all(not row['cookie_present'] for row in rows)
    finally:
        pool.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()
    assert not thread.is_alive() and not pool._runtime._thread.is_alive()
    (tmp_path / 'browser-proxy-proof.json').write_text(json.dumps({
        'results': results, 'proxy_requests': rows, 'pool_and_proxy_closed': True}, indent=2) + '\n')

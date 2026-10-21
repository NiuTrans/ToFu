"""Opt-in real-browser evidence projection; all pages are owned loopback fixtures.

Run with TOFU_SEARCH_TEST_BROWSER=1 and the ordinary Playwright browser runtime.
No external resource, model, credential or site availability is required.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('TOFU_SEARCH_TEST_BROWSER') != '1',
                              reason='opt-in real Playwright runtime')


def test_browser_source_visibility_structure_readiness_and_retirement(tmp_path):
    from tofu_search import SearchConfig, configure, get_playwright_pool

    fact = 'The eastern observatory opened to visitors on 17 May 1986.'
    paragraph = '<p>' + fact + '</p>'
    article = '<main><h1>Observatory archive</h1>' + paragraph + '</main>'
    pages = {
        '/hidden': article + '<div hidden>HIDDEN SENTINEL</div>'
        '<style>.secret{display:none}</style><p class="secret">CSS SENTINEL</p>'
        '<script type="application/json">{"private":"SCRIPT SENTINEL"}</script>',
        '/delayed': '<nav>' + 'Navigation menu and promotional links. ' * 25 + '</nav>'
        '<article>RELATED CARD ONLY: another archive has unrelated records.</article><main></main>'
        '<script>setTimeout(()=>{document.querySelector("main").outerHTML='
        + json.dumps(article) + '},1500);</script>',
        '/visibility': '<main><div style="visibility:hidden">PRIVATE PARENT'
        '<p style="visibility:visible">' + fact + '</p></div></main>',
        '/shadow': '<main><div id="host"></div></main><script>'
        'document.getElementById("host").attachShadow({mode:"open"}).innerHTML='
        + json.dumps('<style>.secret{display:none}</style>' + paragraph
                     + '<p class="secret">SHADOW SECRET</p>') + ';</script>',
        '/slot': '<main><div id="host"><p slot="fact">' + fact + '</p></div></main>'
        '<script>document.getElementById("host").attachShadow({mode:"open"}).innerHTML='
        + json.dumps('<slot name="fact">UNUSED SLOT FALLBACK</slot>') + ';</script>',
        '/structure': '<main>' + paragraph
        + '<p>Read the <a href="/archive">original archive</a> before starting.</p>'
        '<pre>if count &lt; 3:\n    await next_item()\n\n    finish()</pre>'
        '<table><tr><th>Year</th><th>Visitors</th></tr>'
        '<tr><td>1986</td><td>42</td></tr></table></main>',
        '/short': '<main>' + paragraph + '</main>',
        '/empty': '<div id="app"><nav>' + fact * 8 + '</nav><main></main></div>',
        '/bounded': '<main>' + paragraph + '<p>' + 'later source words ' * 2000 + '</p></main>',
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = ('<html><body>' + pages.get(self.path, '') + '</body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    pool, rows = get_playwright_pool(), []
    try:
        for path in pages:
            configure(fetch_max_bytes=500 if path == '/bounded' else 8 * 1024 * 1024)
            url = f'http://127.0.0.1:{server.server_port}' + path
            text = pool.fetch(url, timeout=4, max_chars=0)
            rows.append({'path': path, 'text': text})
            if path == '/empty':
                assert text is None
                continue
            assert text and fact in text, path
            for forbidden in ('HIDDEN SENTINEL', 'CSS SENTINEL', 'SCRIPT SENTINEL',
                              'PRIVATE PARENT', 'SHADOW SECRET', 'UNUSED SLOT FALLBACK', 'setTimeout'):
                assert forbidden not in text, (path, forbidden)
            if path == '/slot':
                assert text.count(fact) == 1
            elif path == '/structure':
                assert f'http://127.0.0.1:{server.server_port}/archive' in text
                assert 'if count < 3:\n    await next_item()\n\n    finish()' in text
                assert '| 1986 | 42 |' in text
            elif path == '/bounded':
                assert text.endswith('\n[…truncated]') and len(text) < 600
        configure(fetch_max_bytes=SearchConfig().fetch_max_bytes)
        text = pool.fetch_authenticated(f'http://127.0.0.1:{server.server_port}/hidden',
                                        cookies=[], timeout=4, max_chars=0)
        assert text and fact in text and 'SENTINEL' not in text
    finally:
        pool.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()
    assert not thread.is_alive()
    (tmp_path / 'rendered-source-proof.json').write_text(json.dumps(rows, indent=2) + '\n')

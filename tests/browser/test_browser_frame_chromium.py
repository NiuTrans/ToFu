"""Real packaged Chrome injections retain and enforce child-document identity."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import zipfile

import pytest
from playwright.sync_api import sync_playwright

from chromium_env import ensure_chromium_env
from tests.support.browser_extension_bundle import build_extension_bundle

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def document_server(document):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = document.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('store', [False, True], ids=['dev', 'store'])
def test_packaged_frame_reads_and_actions_use_only_authorized_document(tmp_path, store):
    archive = build_extension_bundle(ROOT, tmp_path / 'checkout', store=store)
    extension = tmp_path / 'extension'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extension)
    child_html = '''<!doctype html><title>Child document</title>
      <main>Child article with meaningful observed content.</main>
      <button id="action" onclick="document.querySelector('#done').textContent='clicked'">Action</button>
      <p id="done">pending</p>'''
    with document_server(child_html) as child_port:
        child_url = f'http://localhost:{child_port}/child'
        with document_server(f'<title>Parent document</title><iframe src="{child_url}"></iframe>') as parent_port:
            top_url = f'http://127.0.0.1:{parent_port}/main'
            ensure_chromium_env()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch_persistent_context(
                    str(tmp_path / 'profile'), channel='chromium', headless=True,
                    args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'])
                try:
                    worker = (browser.service_workers[0] if browser.service_workers else
                              browser.wait_for_event('serviceworker', timeout=15000))
                    page = browser.new_page()
                    page.goto(top_url)
                    params = worker.evaluate('''async ({top, child}) => {
                      const tab = (await chrome.tabs.query({})).find(tab => tab.url === top);
                      const frame = (await chrome.webNavigation.getAllFrames({tabId: tab.id}))
                        .find(frame => frame.url === child);
                      return {tabId: tab.id, frameId: frame.frameId, expectedDomain: '127.0.0.1'};
                    }''', {'top': top_url, 'child': child_url})
                    state = worker.evaluate('(params) => cmdPageState(params)', params)
                    assert state['url'] == top_url
                    assert state['frame']['url'] == child_url
                    params['expectedDocumentId'] = state['frame']['documentId']
                    worker.evaluate('''({tabId, top}) => _recentNetworkByTab.set(tabId,
                      {pageUrl: top, responses: [{body: 'PARENT_CAPTURE_MARKER'}]})''',
                                    {'tabId': params['tabId'], 'top': top_url})
                    result = worker.evaluate('(params) => cmdReadTab(params)', params)
                    assert result['url'] == child_url
                    assert result['title'] == 'Child document'
                    assert 'Child article' in result['html']
                    assert 'network' not in result
                    assert 'Child article' in result['contentSnapshot']['text']
                    worker.evaluate('(params) => cmdPageClick({...params, selector: "#action"})', params)
                    child = next(frame for frame in page.frames if frame.url == child_url)
                    assert child.locator('#done').inner_text() == 'clicked'
                    child.goto(f'http://localhost:{child_port}/replacement')
                    failure = worker.evaluate('''async params => {
                      try { return await cmdPageExecute({...params, expression: '(window.leaked = true)'}); }
                      catch (error) { return {error: error.message}; }
                    }''', params)
                    assert failure.get('error'), failure
                    assert child.evaluate('window.leaked') is None
                finally:
                    browser.close()

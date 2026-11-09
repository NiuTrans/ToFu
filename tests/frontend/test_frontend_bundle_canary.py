"""The build canary fails on a boot error, not on a slow but healthy boot.

On 2026-11-06 the shared tree's own build needed about 124 s on a loaded host.
Its final Chromium canary waited only 20 s for readiness and 2 s for the
sidebar, so a busy machine would refuse every restart that builds the frontend
(the old server keeps serving, but the restart never happens). The window is
now renewed while the page progresses, and a real error fails at once.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts import frontend_bundle_canary as canary

pytestmark = pytest.mark.unit


def _artifact(tmp_path):
    (tmp_path / 'assets').mkdir()
    entry = 'assets/canary.js'
    (tmp_path / entry).write_text('')
    (tmp_path / 'manifest.json').write_text(
        json.dumps({'frontend/src/main.ts': {'file': entry}}))
    return entry


def _boot_cases():
    ready = """
      setTimeout(() => {
        const sidebar = document.createElement('div');
        sidebar.id = 'sidebar';
        document.body.appendChild(sidebar);
        fetch('/api/v4/conversations');
        fetch('/api/v4/folders');
        window.dispatchEvent(new Event('tofu:app-ready'));
      }, 300);
    """
    slow = """
      let n = 0;
      const tick = () => {
        n += 1;
        const row = document.createElement('span');
        row.textContent = 'working ' + n;
        document.body.appendChild(row);
        if (n < 6) {
          setTimeout(tick, 400);
        } else {
          fetch('/api/v4/conversations');
          fetch('/api/v4/folders');
          window.dispatchEvent(new Event('tofu:app-ready'));
        }
      };
      setTimeout(tick, 200);
    """
    hang = 'window.addEventListener("DOMContentLoaded", () => { for (;;) {} });'
    error = "console.error('[boot] asset manifest rejected');"
    broken = "throw new Error('module graph exploded');"
    return {
        'slow': '<div id="sidebar"></div><script>' + slow + '</script>',
        'ready': '<script>' + ready + '</script>',
        'hang': '<script type="module">' + hang + '</script>',
        'error': '<script>' + error + '</script>',
        'broken': '<script>' + broken + '</script>',
    }


class _Server:
    """A real HTTP origin, so module and API requests actually happen."""

    def __init__(self, case):
        self.case = case

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = _boot_cases()[case].encode() if self.path == '/' else b'{"data":{}}'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def _page(case, window_s=3.0):
    import chromium_env
    chromium_env.ensure_chromium_env()
    executable = chromium_env.chromium_executable()
    if not executable:
        pytest.skip('Chromium is unavailable in this environment')
    from playwright.sync_api import sync_playwright
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True, executable_path=executable,
                                         args=['--no-sandbox', '--disable-dev-shm-usage'])
    server = _Server(case)
    context = browser.new_context(service_workers='block')
    page = context.new_page()
    page_errors, fatal_console, progress = [], [], {'count': 0}
    page.on('pageerror', lambda error: page_errors.append(str(error)))
    page.on('console', lambda message: fatal_console.append(message.text)
            if message.type == 'error' and '[boot]' in message.text else None)
    page.on('console', lambda message: progress.__setitem__('count', progress['count'] + 1))
    ready_flag = {'ready': False}
    page.expose_function('__tofuCanaryProgress', lambda: progress.__setitem__(
        'count', progress['count'] + 1))
    page.expose_function('__tofuCanaryReady', lambda: ready_flag.__setitem__('ready', True))
    page.add_init_script("""
      window.__tofuCanary = { ready: false, unhandled: [] };
      window.addEventListener('tofu:app-ready', () => {
        window.__tofuCanary.ready = true;
        if (window.__tofuCanaryReady) window.__tofuCanaryReady();
      });
      window.addEventListener('error', (event) => {
        window.__tofuCanary.unhandled.push(String(event.error || event.message));
      });
      window.addEventListener('unhandledrejection', (event) => {
        window.__tofuCanary.unhandled.push(String(event.reason));
      });
      if (window.MutationObserver) {
        const observer = new MutationObserver(() => { window.__tofuCanaryProgress(); });
        const observeDocument = () => {
          if (document.documentElement) {
            observer.observe(document.documentElement, {
              childList: true, subtree: true, attributes: true, characterData: true });
          } else if (!window.__tofuCanary.ready) {
            setTimeout(observeDocument, 25);
          }
        };
        observeDocument();
      }
      if (window.fetch) {
        const originalFetch = window.fetch.bind(window);
        window.fetch = (...args) => {
          window.__tofuCanaryProgress();
          return originalFetch(...args).then(
            response => { window.__tofuCanaryProgress(); return response; },
            error => { window.__tofuCanaryProgress(); throw error; });
        };
      }
    """)
    origin = f'http://127.0.0.1:{server.server.server_address[1]}'

    def route_request(route):
        progress['count'] += 1
        if '127.0.0.1' not in route.request.url:
            route.fulfill(status=204, body='')
            return
        shell = route.request.url == origin + '/'
        route.fulfill(
            status=200, content_type='text/html' if shell else 'application/json',
            body=_boot_cases()[case] if shell else json.dumps({'data': {}}),
        )

    page.route('**/*', route_request)
    # Navigate as run_canary does: commit, then the driver-side wait loop.
    page.goto(origin + '/', wait_until='commit', timeout=5000)

    class Resources:
        def __init__(self):
            self.browser, self.server, self.playwright = browser, server, playwright

    return page, progress, page_errors, fatal_console, origin, window_s, Resources(), ready_flag


@pytest.fixture
def small_window(monkeypatch):
    monkeypatch.setattr(canary, 'BOOT_TIMEOUT_MS', 3_000)
    monkeypatch.setattr(canary, '_BOOT_POLL_MS', 250)


def test_a_slow_boot_that_keeps_progressing_passes(tmp_path, small_window):
    page, progress, errors, console, origin, _window, resources, ready_flag = _page('slow')
    try:
        # A sidebar exists only while its boot script is still working.
        canary._await_boot_ready(page, progress, errors, console, ready_flag=ready_flag)
    finally:
        resources.browser.close()
        resources.server.stop()
        resources.playwright.stop()


def test_a_hung_boot_fails_after_one_no_progress_window(tmp_path, small_window):
    page, progress, errors, console, origin, _window, resources, ready_flag = _page('hang')
    try:
        with pytest.raises(canary.BootNoProgress, match='made no progress'):
            canary._await_boot_ready(page, progress, errors, console, ready_flag=ready_flag)
    finally:
        resources.browser.close()
        resources.server.stop()
        resources.playwright.stop()


def test_a_real_boot_error_fails_immediately(tmp_path, small_window):
    page, progress, errors, console, origin, _window, resources, ready_flag = _page('broken')
    try:
        started = __import__('time').monotonic()
        # The waiter stops at once so run_canary reports the real error instead
        # of a timeout.
        canary._await_boot_ready(page, progress, errors, console, ready_flag=ready_flag)
        assert __import__('time').monotonic() - started < 3.0
        assert errors
    finally:
        resources.browser.close()
        resources.server.stop()
        resources.playwright.stop()

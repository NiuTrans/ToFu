"""tests/test_browser_page_preview.py — server-side page preview tool.

Covers lib/browser/preview.py (arg validation, virtual-host fulfilment,
traversal guards, report shaping) and its wiring (dispatch table, tool
schema gate, trusted-input annotation helper). No real Chromium — the
pool is faked behind ``register_task_kind`` / ``_task_q``.
"""

import os
import sys
import types

import pytest

pytestmark = pytest.mark.unit

from lib.browser import preview as pv
from lib.browser.dispatch import BROWSER_HANDLERS, normalize_browser_args
from lib.browser.handlers._interact import _trusted_suffix
from lib.tools import BROWSER_TOOL_NAMES, PAGE_PREVIEW_TOOL_NAMES


# ── arg validation ──────────────────────────────────────────────────

def test_requires_exactly_one_of_path_and_url():
    assert pv.render_page_preview(project_path='/tmp').startswith('Error:')
    both = pv.render_page_preview(project_path='/tmp', path='a.html',
                                  url='http://x.test/')
    assert both.startswith('Error:')


def test_path_mode_requires_a_project():
    out = pv.render_page_preview(project_path=None, path='a.html')
    assert 'requires an attached project' in out


def test_url_mode_rejects_non_http_schemes():
    for bad in ('file:///etc/passwd', 'ftp://x', 'chrome://settings'):
        out = pv.render_page_preview(url=bad)
        assert out.startswith('Error:'), bad


def test_path_mode_rejects_non_html_and_missing(tmp_path):
    (tmp_path / 'notes.txt').write_text('hi')
    out = pv.render_page_preview(project_path=str(tmp_path), path='notes.txt')
    assert 'not an HTML file' in out
    out = pv.render_page_preview(project_path=str(tmp_path), path='gone.html')
    assert 'not found' in out


def test_path_mode_rejects_traversal(tmp_path):
    out = pv.render_page_preview(project_path=str(tmp_path),
                                 path='../../etc/passwd.html')
    assert out.startswith('Error:')


# ── virtual-host fulfilment (pure / fake-route level) ───────────────

def test_resolve_virtual_root_serves_only_inside_the_root(tmp_path):
    (tmp_path / 'ok.css').write_text('body{}')
    got = pv._resolve_virtual_root(str(tmp_path), 'http://tofu-preview.invalid/ok.css')
    assert got and got.endswith('ok.css')
    assert pv._resolve_virtual_root(
        str(tmp_path), 'http://tofu-preview.invalid/../../etc/passwd') is None
    assert pv._resolve_virtual_root(
        str(tmp_path), 'http://tofu-preview.invalid/missing.js') is None


def test_resolve_virtual_root_rejects_symlink_escape(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.html').write_text('<h1>x</h1>')
    root = tmp_path / 'root'
    root.mkdir()
    os.symlink(str(outside / 'secret.html'), str(root / 'link.html'))
    assert pv._resolve_virtual_root(
        str(root), 'http://tofu-preview.invalid/link.html') is None


class _FakeRoute:
    def __init__(self, url, method='GET'):
        self.request = types.SimpleNamespace(url=url, method=method)
        self.outcome = None
        self.kwargs = None

    def fulfill(self, **kwargs):
        self.outcome = 'fulfill'
        self.kwargs = kwargs

    def abort(self):
        self.outcome = 'abort'


def test_virtual_host_fulfiller_serves_files_with_js_mime(tmp_path):
    (tmp_path / 'app.js').write_text('console.log(1)')
    blocked, missing = [], []
    fulfill = pv._virtual_host_fulfiller(str(tmp_path), blocked, missing)
    route = _FakeRoute('http://tofu-preview.invalid/app.js')
    fulfill(route)
    assert route.outcome == 'fulfill'
    assert route.kwargs['status'] == 200
    # ES modules die on a non-JS Content-Type — this is the load-bearing bit.
    assert route.kwargs['content_type'] == 'text/javascript'
    assert b'console.log' in route.kwargs['body']


def test_virtual_host_fulfiller_404s_missing_and_aborts_external(tmp_path):
    blocked, missing = [], []
    fulfill = pv._virtual_host_fulfiller(str(tmp_path), blocked, missing)
    r404 = _FakeRoute('http://tofu-preview.invalid/nope.css')
    fulfill(r404)
    assert r404.kwargs['status'] == 404 and len(missing) == 1
    ext = _FakeRoute('https://cdn.example.com/lib.js')
    fulfill(ext)
    assert ext.outcome == 'abort' and blocked == ['https://cdn.example.com/lib.js']
    post = _FakeRoute('http://tofu-preview.invalid/app.js', method='POST')
    fulfill(post)
    assert post.kwargs['status'] == 405


# ── pool-side render (faked browser, real _do_page_preview) ─────────

class _FakePage:
    def __init__(self):
        self.listeners = {}
        self.routed = None
        self.url = 'http://tofu-preview.invalid/index.html'

    def on(self, event, cb):
        self.listeners[event] = cb

    def route(self, pattern, handler):
        self.routed = (pattern, handler)

    def goto(self, target, timeout, wait_until):
        self.goto_target = target

    def wait_for_timeout(self, ms):
        self.waited = ms

    def title(self):
        return 'Demo'

    def screenshot(self, **kwargs):
        self.shot_kwargs = kwargs
        return b'\xff\xd8\xff' + b'0' * 128  # tiny stand-in JPEG


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.ctx_kwargs = None

    def new_context(self, **kwargs):
        self.ctx_kwargs = kwargs
        self.ctx = _FakeContext(self.page)
        return self.ctx


def test_do_page_preview_file_mode(tmp_path):
    (tmp_path / 'index.html').write_text('<h1>hi</h1>')
    page = _FakePage()
    browser = _FakeBrowser(page)
    res = pv._do_page_preview(browser, {
        'mode': 'file', 'web_root': str(tmp_path), 'entry': 'index.html',
        'width': 1000, 'height': 700, 'full_page': False, 'wait_ms': 5,
    })
    assert res['ok'] is True
    assert res['title'] == 'Demo'
    # virtual host installed + navigated to the entry under it
    assert page.routed is not None and page.routed[0] == '**/*'
    assert page.goto_target == 'http://tofu-preview.invalid/index.html'
    assert browser.ctx_kwargs['viewport'] == {'width': 1000, 'height': 700}
    assert browser.ctx_kwargs['java_script_enabled'] is True
    assert browser.ctx.closed is True
    # listeners wired
    assert 'console' in page.listeners and 'pageerror' in page.listeners
    # console capture flows into the result
    page.listeners  # noqa — captured above
    assert isinstance(res['console'], list)


def test_do_page_preview_url_mode_tracks_failed_requests():
    page = _FakePage()
    browser = _FakeBrowser(page)
    res = pv._do_page_preview(browser, {
        'mode': 'url', 'url': 'http://127.0.0.1:9/dead',
        'width': 800, 'height': 600, 'wait_ms': 0,
    })
    assert res['ok'] is True
    assert page.routed is None  # url mode never installs the virtual host
    assert 'requestfailed' in page.listeners


def test_do_page_preview_nav_error_still_screenshots():
    page = _FakePage()

    def _boom(target, timeout, wait_until):
        raise RuntimeError('net::ERR_CONNECTION_REFUSED')

    page.goto = _boom
    browser = _FakeBrowser(page)
    res = pv._do_page_preview(browser, {
        'mode': 'url', 'url': 'http://127.0.0.1:9/dead', 'wait_ms': 0,
    })
    assert res['ok'] is True
    assert 'ERR_CONNECTION_REFUSED' in res['nav_error']
    assert res['screenshot']  # the error page IS the useful output


# ── caller-side shaping (fake pool module) ──────────────────────────

class _FakeTaskQ:
    def __init__(self, pool):
        self._pool = pool

    def put(self, item):
        (kind, payload), result_q = item
        result_q.put(self._pool.canned)


class _FakePool:
    def __init__(self, canned, ready=True):
        self.canned = canned
        self._ready = ready
        self.kinds = {}
        import queue
        self._task_q = _FakeTaskQ(self)
        self._q = queue  # keep flake quiet about the local import

    def register_task_kind(self, kind, handler):
        self.kinds[kind] = handler

    def _ensure_thread(self):
        return self._ready


@pytest.fixture
def fake_pool_module(monkeypatch):
    """Install a fake tofu_search.fetch.playwright_pool with a canned pool."""

    def _install(canned, ready=True):
        pool = _FakePool(canned, ready=ready)
        mod = types.ModuleType('tofu_search.fetch.playwright_pool')
        mod._pw_pool = pool
        monkeypatch.setitem(sys.modules,
                            'tofu_search.fetch.playwright_pool', mod)
        monkeypatch.setattr(pv, '_registered', False)
        return pool

    return _install


def _ok_result():
    return {
        'ok': True, 'screenshot': b'\xff\xd8\xff' + b'1' * 256,
        'title': 'T', 'url': 'http://tofu-preview.invalid/index.html',
        'console': [{'type': 'error', 'text': 'boom'}],
        'page_errors': ['ReferenceError: x is not defined'],
        'failed_requests': [], 'missing_files': [], 'blocked_external': [],
        'nav_error': None, 'viewport': [1280, 800], 'elapsed_s': 0.3,
    }


def test_render_returns_screenshot_dict_with_report(fake_pool_module):
    fake_pool_module(_ok_result())
    out = pv.render_page_preview(url='http://127.0.0.1:8080/')
    assert isinstance(out, dict) and out.get('__screenshot__') is True
    assert out['dataUrl'].startswith('data:image/jpeg;base64,')
    text = out['_text_fallback']
    assert '[error] boom' in text
    assert 'ReferenceError: x is not defined' in text
    assert 'analyze the screenshot visually' in text


def test_render_reports_pool_unavailable(fake_pool_module):
    fake_pool_module(_ok_result(), ready=False)
    out = pv.render_page_preview(url='http://127.0.0.1:8080/')
    assert out.startswith('Error:') and 'Chromium' in out


def test_render_reports_worker_error(fake_pool_module):
    fake_pool_module({'ok': False, 'error': 'target crashed'})
    out = pv.render_page_preview(url='http://127.0.0.1:8080/')
    assert 'target crashed' in out


def test_report_lists_missing_and_blocked():
    res = _ok_result()
    res['missing_files'] = ['http://tofu-preview.invalid/a.png']
    res['blocked_external'] = ['https://cdn.example.com/x.js']
    text = pv._build_report('index.html', res)
    assert 'does not have' in text and 'a.png' in text
    assert 'blocked' in text and 'cdn.example.com' in text


# ── wiring ──────────────────────────────────────────────────────────

def test_dispatch_table_maps_preview_to_a_local_handler():
    assert 'browser_preview_page' in BROWSER_HANDLERS
    from lib.browser.handlers import _handle_preview_page
    assert BROWSER_HANDLERS['browser_preview_page'] is _handle_preview_page


def test_preview_is_not_in_the_extension_tool_family():
    assert 'browser_preview_page' in PAGE_PREVIEW_TOOL_NAMES
    assert 'browser_preview_page' not in BROWSER_TOOL_NAMES


def test_preview_schema_gate_follows_project_attachment():
    from lib.tools.registry._build import _build_page_preview
    ctx_on = types.SimpleNamespace(project_ready=True, tid='t1')
    ctx_off = types.SimpleNamespace(project_ready=False, tid='t2')
    assert [t['function']['name'] for t in _build_page_preview(ctx_on)] == [
        'browser_preview_page']
    assert _build_page_preview(ctx_off) == []


def test_arg_normalization_covers_preview_keys():
    out = normalize_browser_args({'full_page': True, 'wait_ms': 3000,
                                  'path': 'a.html'})
    assert out['fullPage'] is True and out['waitMs'] == 3000
    assert 'full_page' not in out and 'wait_ms' not in out


# ── trusted-input annotation (server side of the 4.6.0 contract) ────

def test_trusted_suffix_annotation():
    assert _trusted_suffix({'trusted': True}) == ' [trusted CDP input]'
    assert 'synthetic fallback' in _trusted_suffix(
        {'trusted': False, 'fallbackReason': 'devtools attached'})
    assert _trusted_suffix({}) == ''  # pre-4.6.0 extension

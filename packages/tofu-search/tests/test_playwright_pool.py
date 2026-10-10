"""Offline tests for PlaywrightPool internals (no real browser).

Covers the three #10 fixes:
  - _new_context() consolidates context setup (common kwargs, proxy, locale,
    cookie-add, and cookie-failure swallowing).
  - launch failure stops the Playwright driver (no leaked node subprocess)
    and signals _ready_event so waiters unblock.
  - _ensure_thread() waits on _ready_event instead of an unlocked _ready spin.
"""

import asyncio
import sys
import types

import pytest

from tofu_search.fetch.playwright_pool import _DEFAULT_UA, PlaywrightPool

# ── Fakes ──

class FakeContext:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.added_cookies = None
        self.cookie_add_raises = False
        self.closed = False

    def add_cookies(self, cookies):
        if self.cookie_add_raises:
            raise ValueError("bad cookie shape")
        self.added_cookies = cookies

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.cookie_add_raises = False

    def close(self):
        pass  # Controlled browser has no physical process.

    def new_context(self, **kwargs):
        ctx = FakeContext(kwargs)
        ctx.cookie_add_raises = self.cookie_add_raises
        self.contexts.append(ctx)
        return ctx


# ── _new_context ──

def test_new_context_defaults():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    ctx = asyncio.run(pool._new_context(browser))
    assert ctx.kwargs["user_agent"] == _DEFAULT_UA
    assert ctx.kwargs["ignore_https_errors"] is True
    assert ctx.kwargs["java_script_enabled"] is True
    assert "locale" not in ctx.kwargs
    assert "proxy" not in ctx.kwargs
    assert ctx.added_cookies is None


def test_new_context_with_proxy_locale_cookies():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    cookies = [{"name": "s", "value": "1", "domain": "x.com", "path": "/"}]
    ctx = asyncio.run(pool._new_context(browser, cookies=cookies, proxy="http://p:8080",
                            locale="zh-CN"))
    assert ctx.kwargs["proxy"] == {"server": "http://p:8080"}
    assert ctx.kwargs["locale"] == "zh-CN"
    assert ctx.added_cookies == cookies


def test_new_context_swallows_cookie_failure():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    browser.cookie_add_raises = True
    # Must NOT raise — a bad cookie shape should yield a usable context.
    ctx = asyncio.run(pool._new_context(browser, cookies=[{"bad": "shape"}]))
    assert ctx is not None
    assert ctx.added_cookies is None  # add failed, swallowed


def test_new_context_js_disabled():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    ctx = asyncio.run(pool._new_context(browser, java_script_enabled=False))
    assert ctx.kwargs["java_script_enabled"] is False


# ── launch-failure driver cleanup + event signalling ──

class FakeChromium:
    def __init__(self, fail=True):
        self._fail = fail

    def launch(self, **kwargs):
        if self._fail:
            raise RuntimeError("Target page, context or browser has been closed")
        return FakeBrowser()


class FakePlaywright:
    def __init__(self, fail_launch=True):
        self.chromium = FakeChromium(fail=fail_launch)
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeSyncPlaywrightHandle:
    """What sync_playwright() returns; .start() yields the driver object."""
    def __init__(self, pw):
        self._pw = pw

    def start(self):
        return self._pw

    def __enter__(self):
        return self._pw

    def __exit__(self, *args):
        self._pw.stop()


@pytest.fixture
def fake_playwright_module(monkeypatch):
    """Install a fake 'playwright.sync_api' so _worker_loop imports our stub."""
    created = {}

    def _make(fail_launch):
        pw = FakePlaywright(fail_launch=fail_launch)
        created["pw"] = pw

        mod = types.ModuleType("playwright.sync_api")
        mod.sync_playwright = lambda: FakeSyncPlaywrightHandle(pw)
        pkg = types.ModuleType("playwright")
        async_mod = types.ModuleType('playwright.async_api')
        async_mod.async_playwright = lambda: FakeSyncPlaywrightHandle(pw)
        monkeypatch.setitem(sys.modules, 'playwright.async_api', async_mod)
        pkg.sync_api = mod
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
        return pw

    return _make, created


def test_worker_loop_launch_failure_stops_driver(fake_playwright_module):
    make, _ = fake_playwright_module
    pw = make(fail_launch=True)
    pool = PlaywrightPool()
    async def probe(browser): return True
    try:
        with pytest.raises(RuntimeError):pool._get_runtime().submit(probe).result(5)
        assert pool._ready is False and pool._ready_event.is_set()
        assert pw.stopped is True
    finally:pool._shutdown()


def test_worker_loop_launch_failure_drains_pending_tasks(fake_playwright_module):
    make, _ = fake_playwright_module
    make(fail_launch=True)
    pool = PlaywrightPool()
    async def probe(browser): return True
    calls=[pool._get_runtime().submit(probe) for _ in range(32)]
    try:
        for call in calls:
            with pytest.raises(RuntimeError):call.result(5)
    finally:pool._shutdown()
    assert all(call.done() for call in calls)


def test_worker_loop_success_sets_ready(fake_playwright_module):
    make, created = fake_playwright_module
    make(fail_launch=False)
    pool = PlaywrightPool()
    async def probe(browser): return True
    try:
        assert pool._get_runtime().submit(probe).result(5)
        assert pool._ready is True and pool._ready_event.is_set()
    finally:pool._shutdown()
    assert created['pw'].stopped is True and pool._ready is False


# ── register_task_kind (0.6.1) — host apps ride the pool ──

def test_register_task_kind_rejects_builtin_and_conflicting_handlers():
    pool = PlaywrightPool()
    with pytest.raises(ValueError):
        pool.register_task_kind('pdf_render', lambda b, p: None)
    with pytest.raises(ValueError):
        pool.register_task_kind('', lambda b, p: None)
    with pytest.raises(ValueError):
        pool.register_task_kind('page_x', 'not-callable')

    handler = lambda b, p: {'ok': True}  # noqa: E731
    pool.register_task_kind('page_x', handler)
    pool.register_task_kind('page_x', handler)  # same handler → idempotent
    with pytest.raises(ValueError):
        pool.register_task_kind('page_x', lambda b, p: {'ok': False})


def test_worker_loop_dispatches_registered_kind(fake_playwright_module):
    make, _ = fake_playwright_module
    make(fail_launch=False)
    pool = PlaywrightPool()
    seen = {}
    def handler(browser,payload):
        seen['browser']=browser
        return {'echo':payload}
    pool.register_task_kind('page_preview',handler)
    try:assert pool.submit_task('page_preview',{'w':1280}).result(5)=={'echo':{'w':1280}}
    finally:pool._shutdown()
    assert isinstance(seen['browser'],FakeBrowser)


def test_worker_loop_unknown_kind_still_returns_none(fake_playwright_module):
    make, _ = fake_playwright_module
    make(fail_launch=False)
    pool=PlaywrightPool()
    try:assert pool.submit_task('nope',{}).result(5) is None
    finally:pool._shutdown()


def test_async_page_calls_overlap_and_retire_every_context(fake_playwright_module):
    import threading
    make, created = fake_playwright_module
    pw = make(fail_launch=False)
    entered, release = threading.Event(), threading.Event()
    contexts=[]
    active=peak=0
    class Context:
        def __init__(self):
            nonlocal active, peak
            active+=1
            peak=max(peak,active)
            contexts.append(self)
        async def add_cookies(self, cookies): pass
        async def new_page(self): return Page()
        async def close(self):
            nonlocal active
            active-=1
    class Locator:
        async def text_content(self, **kwargs): return 'readable page body '*20
    class Page:
        async def route(self, *args): pass
        async def goto(self, url, **kwargs):
            if not url.endswith('/healthy'):
                if len(contexts)==32: entered.set()
                while not release.is_set(): await asyncio.sleep(.01)
        async def wait_for_function(self, *args, **kwargs): pass
        async def wait_for_timeout(self, *args): pass
        def locator(self, *args): return Locator()
        async def evaluate(self, expression):
            return 400 if expression.endswith('length') else 'readable page body '*20
        async def content(self): return '<p>readable page body</p>'
    class Browser:
        async def new_context(self, **kwargs): return Context()
        async def close(self): pass
    pw.chromium.launch=lambda **kwargs:Browser()
    pool=PlaywrightPool()
    calls=[]
    try:
        calls=[pool._submit_payload((f'https://pages.example/{i}',5,0)) for i in range(32)]
        assert entered.wait(5)
        assert pool._submit_payload(('https://pages.example/healthy',5,0)).result(2)
        assert active==32 and peak==33
    finally:
        release.set()
        pool.shutdown()
    assert active==0 and len(contexts)==33
    assert all(call.done() for call in calls)
    assert pw.stopped is True


def test_failed_startup_retries_only_after_owned_generation_closes(fake_playwright_module, monkeypatch):
    import time

    import tofu_search.fetch.playwright_pool as module
    make, _ = fake_playwright_module
    first = make(fail_launch=True)
    monkeypatch.setattr(module,'HAS_PLAYWRIGHT',True)
    pool=PlaywrightPool()
    try:
        assert pool.ensure_ready() is False
        assert first.stopped is True
        previous=pool._runtime
        pool._last_fail_ts=time.time()-61
        second=make(fail_launch=False)
        assert pool.ensure_ready() is True
        assert previous.snapshot()=={'active_calls':0,'owner_threads':0}
        assert pool._last_fail_ts==0
    finally:pool.shutdown()
    assert second.stopped is True


def test_legacy_handlers_start_independently_and_keep_browser_thread_affinity(monkeypatch):
    import threading
    started, stopped = [], []
    barrier=threading.Barrier(33)
    release=threading.Event()
    class Browser:
        def __init__(self): self.owner=threading.get_ident()
        def close(self): assert self.owner==threading.get_ident()
    class Driver:
        def __init__(self):
            self.owner=threading.get_ident()
            self.chromium=self
            started.append(self)
        def launch(self, **kwargs):
            assert self.owner==threading.get_ident()
            return Browser()
        def __enter__(self): return self
        def __exit__(self,*args):
            assert self.owner==threading.get_ident()
            stopped.append(self)
    module=types.ModuleType('playwright.sync_api')
    module.sync_playwright=Driver
    monkeypatch.setitem(sys.modules,'playwright.sync_api',module)
    pool=PlaywrightPool()
    def handler(browser,payload):
        assert browser.owner==threading.get_ident()
        if payload!='healthy':
            barrier.wait(5)
            assert release.wait(10)
        return payload
    pool.register_task_kind('legacy',handler)
    calls=[]
    try:
        calls=[pool.submit_task('legacy',i) for i in range(32)]
        barrier.wait(5)
        assert pool.submit_task('legacy','healthy').result(2)=='healthy'
    finally:
        release.set()
        pool.shutdown()
    assert all(call.done() for call in calls)
    assert len(started)==len(stopped)==33
    assert pool._runtime._resource_task is None

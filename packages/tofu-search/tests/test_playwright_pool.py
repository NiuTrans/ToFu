"""Offline tests for PlaywrightPool internals (no real browser).

Covers the three #10 fixes:
  - _new_context() consolidates context setup (common kwargs, proxy, locale,
    cookie-add, and cookie-failure swallowing).
  - launch failure stops the Playwright driver (no leaked node subprocess)
    and signals _ready_event so waiters unblock.
  - _ensure_thread() waits on _ready_event instead of an unlocked _ready spin.
"""

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

    def new_context(self, **kwargs):
        ctx = FakeContext(kwargs)
        ctx.cookie_add_raises = self.cookie_add_raises
        self.contexts.append(ctx)
        return ctx


# ── _new_context ──

def test_new_context_defaults():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    ctx = pool._new_context(browser)
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
    ctx = pool._new_context(browser, cookies=cookies, proxy="http://p:8080",
                            locale="zh-CN")
    assert ctx.kwargs["proxy"] == {"server": "http://p:8080"}
    assert ctx.kwargs["locale"] == "zh-CN"
    assert ctx.added_cookies == cookies


def test_new_context_swallows_cookie_failure():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    browser.cookie_add_raises = True
    # Must NOT raise — a bad cookie shape should yield a usable context.
    ctx = pool._new_context(browser, cookies=[{"bad": "shape"}])
    assert ctx is not None
    assert ctx.added_cookies is None  # add failed, swallowed


def test_new_context_js_disabled():
    pool = PlaywrightPool()
    browser = FakeBrowser()
    ctx = pool._new_context(browser, java_script_enabled=False)
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
        pkg.sync_api = mod
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
        return pw

    return _make, created


def test_worker_loop_launch_failure_stops_driver(fake_playwright_module):
    make, created = fake_playwright_module
    pw = make(fail_launch=True)

    pool = PlaywrightPool()
    import queue as q
    task_q = q.Queue()
    # Run the worker loop synchronously — it should return after launch fails.
    pool._worker_loop(task_q)

    assert pool._ready is False
    assert pool._ready_event.is_set()      # waiters unblocked
    assert pw.stopped is True              # driver subprocess cleaned up (no leak)


def test_worker_loop_launch_failure_drains_pending_tasks(fake_playwright_module):
    make, created = fake_playwright_module
    make(fail_launch=True)

    pool = PlaywrightPool()
    import queue as q
    task_q = q.Queue()
    result_q = q.Queue()
    task_q.put((("some", "payload"), result_q))  # a waiter already queued

    pool._worker_loop(task_q)

    # The pending waiter must have received None, not hang forever.
    assert result_q.get_nowait() is None


def test_worker_loop_success_sets_ready(fake_playwright_module):
    make, created = fake_playwright_module
    make(fail_launch=False)

    pool = PlaywrightPool()
    import queue as q
    task_q = q.Queue()
    task_q.put(None)  # sentinel → loop exits cleanly right after launch

    pool._worker_loop(task_q)

    assert pool._ready is True
    assert pool._ready_event.is_set()
    assert created["pw"].stopped is True   # clean shutdown stops driver too


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
    make, created = fake_playwright_module
    make(fail_launch=False)

    pool = PlaywrightPool()
    seen = {}

    def _handler(browser, payload):
        seen['browser'] = browser
        seen['payload'] = payload
        return {'echo': payload}

    pool.register_task_kind('page_preview', _handler)

    import queue as q
    task_q = q.Queue()
    result_q = q.Queue()
    task_q.put((('page_preview', {'w': 1280}), result_q))
    task_q.put(None)  # sentinel

    pool._worker_loop(task_q)

    assert result_q.get_nowait() == {'echo': {'w': 1280}}
    assert isinstance(seen['browser'], FakeBrowser)
    assert seen['payload'] == {'w': 1280}


def test_worker_loop_unknown_kind_still_returns_none(fake_playwright_module):
    """The pre-registry behaviour must survive: unknown kinds warn + None."""
    make, created = fake_playwright_module
    make(fail_launch=False)

    pool = PlaywrightPool()
    import queue as q
    task_q = q.Queue()
    result_q = q.Queue()
    task_q.put((('nope', {}), result_q))
    task_q.put(None)

    pool._worker_loop(task_q)

    assert result_q.get_nowait() is None

"""Anonymous browser reads preserve the caller's selected network route."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tofu_search.config import SearchConfig, bind_call_config
from tofu_search.fetch import playwright_pool, rendered_source
from tofu_search.search.proxy_mode import _reset_proxy_mode_manager, proxy_mode_manager

URL = 'https://archive.example/read'
PROXY = 'http://proxy.example:8080'
FACT = 'The eastern observatory opened on 17 May 1986.'


@pytest.fixture(autouse=True)
def isolated_routes(monkeypatch):
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    _reset_proxy_mode_manager()
    yield
    _reset_proxy_mode_manager()


@pytest.fixture
def browser(monkeypatch):
    class Page:
        url = URL
        main_frame = object()

        def __init__(self):
            self.listeners = {'request': [], 'response': []}

        def on(self, event, callback):
            self.listeners[event].append(callback)

        def remove_listener(self, event, callback):
            self.listeners[event].remove(callback)

        async def goto(self, url, **kwargs):
            self.url = url
            request = SimpleNamespace(frame=self.main_frame, is_navigation_request=lambda: True)
            for listener in self.listeners['request']:
                listener(request)
            for listener in self.listeners['response']:
                listener(SimpleNamespace(status=200, frame=self.main_frame, request=request))

        async def route(self, *args):
            pass

    class Context:
        def __init__(self, options):
            self.options = options
            self.cookies = []
            self.closed = False

        async def new_page(self):
            return Page()

        async def add_cookies(self, cookies):
            self.cookies = cookies

        async def close(self):
            self.closed = True

    class Browser:
        contexts = None

        def __init__(self):
            self.contexts = []

        async def new_context(self, **kwargs):
            context = Context(kwargs)
            self.contexts.append(context)
            await asyncio.sleep(0)
            return context

    async def wait(page, **kwargs):
        pass

    async def extract(page, max_chars, url, **kwargs):
        return FACT

    monkeypatch.setattr(rendered_source, 'wait_for_rendered_source', wait)
    monkeypatch.setattr(rendered_source, 'extract_rendered_source', extract)
    value = Browser()
    yield value
    assert all(context.closed for context in value.contexts)


def read(browser, config=None, url=URL):
    with bind_call_config(config or SearchConfig()):
        return asyncio.run(playwright_pool.PlaywrightPool()._do_fetch(browser, url, 5, 0))


def test_explicit_proxy_reaches_anonymous_context(browser, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://ambient.example:9090')
    monkeypatch.setenv('NO_PROXY', '*')
    assert read(browser, SearchConfig(proxy_url=PROXY, proxy_dual_attempt=False)) == FACT
    assert browser.contexts[0].options['proxy'] == {'server': PROXY}
    assert not browser.contexts[0].cookies


@pytest.mark.parametrize('name', ['https_proxy', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'])
def test_environment_proxy_reaches_anonymous_context(browser, monkeypatch, name):
    monkeypatch.setenv(name, PROXY)
    assert read(browser) == FACT
    assert browser.contexts[0].options['proxy'] == {'server': PROXY}


def test_environment_proxy_selection_is_scheme_specific(browser, monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://http-route.example:8080')
    monkeypatch.setenv('HTTPS_PROXY', PROXY)
    assert read(browser, url='http://archive.example/read') == FACT
    assert browser.contexts[0].options['proxy']['server'] == 'http://http-route.example:8080'


@pytest.mark.parametrize('bypass', ['archive.example', '.example', '*'])
def test_environment_no_proxy_bypasses_the_initial_destination(browser, monkeypatch, bypass):
    monkeypatch.setenv('HTTPS_PROXY', PROXY)
    monkeypatch.setenv('NO_PROXY', bypass)
    assert read(browser) == FACT
    assert 'proxy' not in browser.contexts[0].options


def test_no_proxy_configured_keeps_one_direct_context(browser):
    assert read(browser) == FACT
    assert len(browser.contexts) == 1
    assert 'proxy' not in browser.contexts[0].options


@pytest.mark.parametrize('preferred, expected', [('direct', None), ('proxy#1', 'http://backup:8081')])
def test_http_successful_route_is_reused_without_another_retry_owner(browser, preferred, expected):
    proxy_mode_manager.record_success('fetch:archive.example', preferred)
    config = SearchConfig(proxy_url=PROXY, proxy_fallback_urls=['http://backup:8081'])
    assert read(browser, config) == FACT
    assert len(browser.contexts) == 1
    assert browser.contexts[0].options.get('proxy') == ({'server': expected} if expected else None)
    assert proxy_mode_manager.status()['fetch:archive.example']['mode'] == preferred


def test_forced_proxy_ignores_previously_learned_direct_route(browser):
    proxy_mode_manager.record_success('fetch:archive.example', 'direct')
    assert read(browser, SearchConfig(proxy_url=PROXY, proxy_dual_attempt=False)) == FACT
    assert browser.contexts[0].options['proxy'] == {'server': PROXY}


def test_http_proxy_userinfo_uses_browser_authentication_fields(browser):
    assert read(browser, SearchConfig(proxy_url='http://reader%40team:p%3Ass@proxy.example:8080')) == FACT
    assert browser.contexts[0].options['proxy'] == {
        'server': PROXY, 'username': 'reader@team', 'password': 'p:ss'}


@pytest.mark.parametrize('value, expected', [
    ('proxy.example:8080', PROXY),
    ('https://[::1]:8443', 'https://[::1]:8443'),
    ('socks5h://proxy.example:1080', 'socks5://proxy.example:1080'),
])
def test_supported_proxy_addresses(browser, value, expected):
    assert read(browser, SearchConfig(proxy_url=value)) == FACT
    assert browser.contexts[0].options['proxy'] == {'server': expected}


@pytest.mark.parametrize('value', [
    'ftp://reader:secret-sentinel@proxy.example:21',
    'http://reader:secret-sentinel@proxy.example:invalid',
    'socks5://reader:secret-sentinel@proxy.example:1080',
    'http://reader:secret-sentinel@/missing-host',
])
def test_invalid_or_unsupported_proxy_fails_without_direct_egress_or_secret_log(browser, caplog, value):
    with caplog.at_level('DEBUG'):
        assert read(browser, SearchConfig(proxy_url=value)) is None
    assert not browser.contexts
    assert 'secret-sentinel' not in caplog.text


@pytest.mark.parametrize('source_proxy', ['', 'http://identity.example:9000'])
def test_authenticated_context_keeps_only_source_route(browser, monkeypatch, source_proxy):
    monkeypatch.setenv('HTTPS_PROXY', PROXY)
    cookies = [{'name': 'session', 'value': 'owned-session', 'domain': 'archive.example', 'path': '/'}]
    with bind_call_config(SearchConfig(proxy_url='http://search-only:8080')):
        result = asyncio.run(playwright_pool.PlaywrightPool()._do_fetch_authenticated(browser, {
            'url': URL, 'cookies': cookies, 'proxy': source_proxy, 'timeout': 5}))
    assert result == FACT
    assert browser.contexts[0].cookies == cookies
    assert browser.contexts[0].options.get('proxy') == ({'server': source_proxy} if source_proxy else None)


def test_concurrent_public_pool_calls_keep_request_config_through_owner_loop(browser, monkeypatch):
    @asynccontextmanager
    async def resource():
        yield browser

    pool = playwright_pool.PlaywrightPool()
    monkeypatch.setattr(pool, '_browser_resource', resource)
    try:
        calls = []
        for index in range(5):
            with bind_call_config(SearchConfig(proxy_url=f'http://route-{index}.example:8080')):
                calls.append(pool.submit_task('fetch', (URL, 5, 0)))
        assert [call.result(5) for call in calls] == [FACT] * 5
    finally:
        pool.shutdown()
    assert {c.options['proxy']['server'] for c in browser.contexts} == {
        f'http://route-{i}.example:8080' for i in range(5)}
    assert not pool._runtime._thread.is_alive()

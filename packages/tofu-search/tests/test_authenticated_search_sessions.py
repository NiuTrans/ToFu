"""Compatibility reuse belongs to one captured credential/provider lifetime."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from copy import deepcopy
from threading import Event

import pytest

import tofu_search
from tofu_search import providers
from tofu_search.search.engines import xhs

pytestmark = pytest.mark.unit


def cards(label, count=5):
    return [{'title': f'{label} {index}', 'url': f'https://example.test/{label}/{index}',
             'snippet': f'Owned search catalogue for {label} with source details. ' * 5}
            for index in range(count)]


class Source(tofu_search.AuthSourceProvider):
    def __init__(self, identity='first'):
        self.row = {'enabled': True, 'access_strategy': 'browser_first',
                    'cookies': [{'name': 'session', 'value': identity}], 'proxy': ''}

    def get_source(self, domain):
        assert domain == 'xiaohongshu.com'
        return self.row


class Browser(tofu_search.BrowserProvider):
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def is_connected(self):
        return True

    def scrape(self, url, **kwargs):
        self.calls += 1
        return self.result


@pytest.fixture
def rig(monkeypatch):
    from tofu_search.fetch.playwright_pool import _pw_pool

    tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=600,
                          xhs_backoff_cooldown_s=0)
    source, calls = Source(), []
    providers.register_auth_source_provider(source)

    def replay(url, *, cookies, proxy='', **kwargs):
        calls.append((deepcopy(cookies), proxy))
        return cards(cookies[0]['value'])

    monkeypatch.setattr(_pw_pool, 'search_authenticated', replay)
    return source, calls, _pw_pool


def test_browser_result_reenters_current_read_authority(rig):
    browser = Browser(cards('allowed'))
    providers.register_browser_provider(browser)
    assert xhs.search_xhs('query')[0]['title'] == 'allowed 0'
    browser.result = []
    assert xhs.search_xhs('query') == []
    assert browser.calls == 2
    assert rig[1] == []


@pytest.mark.parametrize('first_result', ['positive', 'cooldown'])
def test_replaced_browser_never_borrows_previous_state(rig, first_result):
    tofu_search.configure(xhs_backoff_cooldown_s=600)
    first = Browser(cards('first') if first_result == 'positive' else [])
    providers.register_browser_provider(first)
    for _ in range(3 if first_result == 'cooldown' else 1):
        xhs.search_xhs('query')
    second = Browser(cards('second'))
    providers.register_browser_provider(second)
    assert xhs.search_xhs('query')[0]['title'] == 'second 0'
    assert second.calls == 1


@pytest.mark.parametrize('change', ['cookies', 'proxy', 'provider', 'disabled'])
def test_replay_authority_change_retires_old_reuse(rig, change):
    source, calls, pool = rig
    assert xhs.search_xhs('query')[0]['title'] == 'first 0'
    if change == 'cookies':
        source.row['cookies'][0]['value'] = 'second'
    elif change == 'proxy':
        source.row['proxy'] = 'http://new-route.example:8080'
    elif change == 'provider':
        providers.register_auth_source_provider(Source())
    else:
        source.row['enabled'] = False
        assert xhs.search_xhs('query') == []
        source.row['enabled'] = True
    pool.search_authenticated = lambda *args, **kwargs: calls.append(kwargs) or cards('second')
    assert xhs.search_xhs('query')[0]['title'] == 'second 0'
    assert len(calls) == 2


def test_replay_cache_preserves_requested_result_count(rig):
    assert len(xhs.search_xhs('query', max_results=2)) == 2
    assert len(xhs.search_xhs('query', max_results=5)) == 5
    assert len(rig[1]) == 2


@pytest.mark.parametrize('cached', [False, True])
def test_mutating_returned_rows_cannot_change_reused_results(rig, cached):
    if cached:
        xhs.search_xhs('query')
    rows = xhs.search_xhs('query')
    rows[0]['title'] = 'caller changed this'
    rows.clear()
    assert xhs.search_xhs('query')[0]['title'] == 'first 0'
    assert len(rig[1]) == 1


def test_browser_fallback_cannot_mutate_captured_replay_credentials(rig):
    source, calls, _pool = rig

    class MutatingBrowser(Browser):
        def scrape(self, url, **kwargs):
            source.row['cookies'][0]['value'] = 'second'
            return None

    providers.register_browser_provider(MutatingBrowser(None))
    assert xhs.search_xhs('query')[0]['title'] == 'first 0'
    assert calls[0][0][0]['value'] == 'first'
    assert source.row['cookies'][0]['value'] == 'second'


def test_retired_replay_completion_cannot_publish_into_current_session(rig, monkeypatch):
    source, _calls, pool = rig
    started, release = Event(), Event()
    calls = []

    def replay(url, *, cookies, **kwargs):
        identity = cookies[0]['value']
        calls.append(identity)
        if identity == 'first':
            started.set()
            assert release.wait(10)
        return cards(identity)

    monkeypatch.setattr(pool, 'search_authenticated', replay)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(xhs.search_xhs, 'query')
        try:
            assert started.wait(10)
            source.row['cookies'] = [{'name': 'session', 'value': 'second'}]
            assert xhs.search_xhs('query')[0]['title'] == 'second 0'
        finally:
            release.set()
        assert pending.result()[0]['title'] == 'first 0'
    assert xhs.search_xhs('query')[0]['title'] == 'second 0'
    assert calls == ['first', 'second']


@pytest.mark.parametrize('lookup_fails', [False, True])
def test_delayed_source_lookup_cannot_retire_newer_session(rig, lookup_fails):
    source, calls, _pool = rig
    assert xhs.search_xhs('query')[0]['title'] == 'first 0'
    started, release = Event(), Event()

    def lookup(domain):
        snapshot = deepcopy(source.row)
        if snapshot['cookies'][0]['value'] == 'first':
            started.set()
            assert release.wait(10)
            if lookup_fails:
                raise RuntimeError('old source unavailable')
        return snapshot

    source.get_source = lookup
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(xhs.search_xhs, 'query')
        try:
            assert started.wait(10)
            source.row['cookies'][0]['value'] = 'second'
            assert xhs.search_xhs('query')[0]['title'] == 'second 0'
        finally:
            release.set()
        assert [row['title'] for row in pending.result()] == (
            [] if lookup_fails else [row['title'] for row in cards('first')])
    assert xhs.search_xhs('query')[0]['title'] == 'second 0'
    assert len(calls) == 2


@pytest.mark.parametrize('first_transport', ['browser', 'replay'])
def test_cooldown_belongs_to_the_transport_that_observed_empty_results(
        rig, monkeypatch, first_transport):
    _source, _calls, pool = rig
    tofu_search.configure(xhs_backoff_cooldown_s=600)
    if first_transport == 'browser':
        first = Browser([])
        providers.register_browser_provider(first)
    else:
        monkeypatch.setattr(pool, 'search_authenticated', lambda *a, **kw: [])
    for index in range(3):
        assert xhs.search_xhs(f'empty {index}') == []
    if first_transport == 'browser':
        providers.register_browser_provider(None)
        expected = 'first 0'
    else:
        providers.register_browser_provider(Browser(cards('browser')))
        expected = 'browser 0'
    assert xhs.search_xhs('fresh query')[0]['title'] == expected


def test_disabled_source_cannot_supply_replay_after_native_unavailable(rig):
    source, calls, _pool = rig
    source.row['enabled'] = False

    class Native(tofu_search.SiteSearchProvider):
        def list_sources(self):
            return [{'id': 'xiaohongshu', 'name': 'Xiaohongshu'}]

        def search(self, source_id, query, **kwargs):
            return None

    providers.register_site_search_provider(Native())
    assert xhs.search_xhs('query') == []
    assert calls == []


def test_source_failure_retires_reuse_until_a_fresh_source_read(rig, monkeypatch):
    source, calls, _pool = rig
    assert xhs.search_xhs('query')
    with monkeypatch.context() as patch:
        def fail(domain):
            raise RuntimeError('source unavailable')
        patch.setattr(source, 'get_source', fail)
        assert xhs.search_xhs('query') == []
    assert xhs.search_xhs('query')
    assert len(calls) == 2


def test_auth_binding_is_captured_before_engine_worker_dispatch(rig):
    from tofu_search.search.orchestrator import perform_web_search
    identity = ContextVar('fixture_auth_identity', default='unbound')
    bindings = []

    class Template(Source):
        def bind(self):
            bindings.append(identity.get())
            return Source(identity.get())

        def get_source(self, domain):
            raise AssertionError('unbound auth source was used')

    providers.register_auth_source_provider(Template())
    token = identity.set('owned')
    try:
        rows = perform_web_search('owned catalogue', engines=['Xiaohongshu'],
                                  fetch_pages=False, filter_pages=False, rerank=False)
    finally:
        identity.reset(token)
    assert rows and all(row['title'].startswith('owned ') for row in rows)
    assert bindings == ['owned']


def test_registered_replacement_does_not_change_an_active_source_binding(rig):
    original = providers.get_auth_source_provider()
    with providers.bound_optional_providers():
        providers.register_auth_source_provider(Source('second'))
        assert providers.get_auth_source_provider() is original
    assert providers.get_auth_source_provider() is not original


def test_auth_binding_failure_restores_all_previous_provider_views(rig):
    original = Browser(cards('original'))
    bound = Browser(cards('bound'))
    original.bind = lambda: bound

    class Broken(Source):
        def bind(self):
            raise RuntimeError('binding failure')

    source = Broken()
    providers.register_browser_provider(original)
    providers.register_auth_source_provider(source)
    with pytest.raises(RuntimeError, match='binding failure'):
        with providers.bound_optional_providers():
            pytest.fail('failed binding entered a public operation')
    assert providers.get_browser_provider() is original
    assert providers.get_auth_source_provider() is source

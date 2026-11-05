"""Native failures are authoritative, not permission for cookie replay."""

from unittest.mock import Mock

import pytest

import tofu_search
from tofu_search import providers
from tofu_search._provider_scope import auth_source_scope
from tofu_search.config import SearchConfig
from tofu_search.search import orchestrator
from tofu_search.search.engines import xhs
from tofu_search.search.engines._xhs_guard import _BACKOFF_AFTER_EMPTY, RiskGuard
from tofu_search.search.engines._xhs_sessions import capture_source

pytestmark = pytest.mark.unit


class NativeProvider(tofu_search.SiteSearchProvider):
    def __init__(self, outcome):
        self.outcome = outcome

    def list_sources(self):
        return [{'id': 'xiaohongshu', 'name': 'Xiaohongshu'},
                {'id': 'catalog', 'name': 'Catalog'}]

    def search(self, source_id, query, **kwargs):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def rig(monkeypatch):
    from tofu_search.fetch.playwright_pool import _pw_pool

    browser = Mock(spec=tofu_search.BrowserProvider)
    browser.bind.return_value = browser
    browser.is_connected.return_value = True
    browser.scrape.return_value = None
    auth = Mock(spec=tofu_search.AuthSourceProvider)
    auth.bind.return_value = auth
    auth.get_source.return_value = {
        'enabled': True, 'cookies': [{'name': 'session', 'value': 'fake'}],
        'access_strategy': 'browser_first',
    }
    pool = Mock(return_value=[])
    monkeypatch.setattr(_pw_pool, 'search_authenticated', pool)
    providers.register_browser_provider(browser)
    providers.register_auth_source_provider(auth)
    providers.register_site_search_provider(None)
    monkeypatch.setattr(xhs, 'get_config', lambda: SearchConfig(
        xhs_min_interval_s=0, xhs_cache_ttl_s=0, xhs_backoff_cooldown_s=0))
    return browser, pool


def test_public_terminal_error_stops_compat_and_pool(monkeypatch, rig):
    assert tofu_search.SiteSearchTerminalError is providers.SiteSearchTerminalError
    error = tofu_search.SiteSearchTerminalError()
    providers.register_site_search_provider(NativeProvider(error))
    assert xhs.xhs_search_available()
    with pytest.raises(tofu_search.SiteSearchTerminalError) as caught:
        xhs.search_xhs('private query')
    assert caught.value is error
    assert 'private query' not in str(error)
    rig[0].scrape.assert_not_called()
    rig[1].assert_not_called()


@pytest.mark.parametrize('engine', ['Xiaohongshu', 'Site:catalog'])
def test_explicit_orchestrator_reports_terminal_engine_error(monkeypatch, rig, engine):
    providers.register_site_search_provider(NativeProvider(
        tofu_search.SiteSearchTerminalError()))
    result = orchestrator.perform_web_search(
        'private query', engines=[engine], fetch_pages=False,
        filter_pages=False, rerank=False)
    assert list(result) == []
    assert engine in result._search_diag['engine_errors']
    assert result._search_diag['engine_empty'] == []
    rig[0].scrape.assert_not_called()
    rig[0].search.assert_not_called()
    rig[1].assert_not_called()


def test_native_empty_is_real_empty_without_fallback(monkeypatch, rig):
    providers.register_site_search_provider(NativeProvider([]))
    assert xhs.search_xhs('query') == []
    rig[0].scrape.assert_not_called()
    rig[1].assert_not_called()


@pytest.mark.parametrize('outcome', [None, RuntimeError('legacy unavailable')])
def test_unavailable_and_legacy_errors_keep_fallback(monkeypatch, rig, outcome):
    providers.register_site_search_provider(NativeProvider(outcome))
    assert xhs.search_xhs('query') == []
    rig[0].scrape.assert_called_once()
    rig[1].assert_called_once()


def test_standalone_unavailable_stays_inert(monkeypatch, rig):
    providers.register_browser_provider(None)
    providers.register_auth_source_provider(None)
    assert not xhs.xhs_search_available()
    assert xhs.search_xhs('query') == []
    rig[1].assert_not_called()


@pytest.mark.parametrize('next_outcome', ['result', 'empty', 'denied'])
def test_each_native_call_reenters_its_own_authority(monkeypatch, rig, next_outcome):
    """A previous browser's result cannot replace this invocation's decision."""
    monkeypatch.setattr(xhs, 'get_config', lambda: SearchConfig(
        xhs_min_interval_s=0, xhs_cache_ttl_s=600, xhs_backoff_cooldown_s=600))
    first = NativeProvider([{'title': 'Owner one', 'url': 'https://example.test/one'}])
    first.search = Mock(wraps=first.search)
    providers.register_site_search_provider(first)
    assert xhs.search_xhs('same query')[0]['title'] == 'Owner one'

    outcome = {
        'result': [{'title': 'Owner two', 'url': 'https://example.test/two'}],
        'empty': [],
        'denied': tofu_search.SiteSearchTerminalError(),
    }[next_outcome]
    second = NativeProvider(outcome)
    second.search = Mock(wraps=second.search)
    providers.register_site_search_provider(second)
    if next_outcome == 'denied':
        with pytest.raises(tofu_search.SiteSearchTerminalError):
            xhs.search_xhs('same query')
    else:
        actual = xhs.search_xhs('same query')
        assert [row['title'] for row in actual] == (
            ['Owner two'] if next_outcome == 'result' else [])
    first.search.assert_called_once()
    second.search.assert_called_once()
    rig[0].scrape.assert_not_called()
    rig[1].assert_not_called()


def test_legacy_cooldown_cannot_suppress_native_admission(monkeypatch, rig):
    monkeypatch.setattr(xhs, 'get_config', lambda: SearchConfig(
        xhs_min_interval_s=0, xhs_cache_ttl_s=600, xhs_backoff_cooldown_s=600))
    for index in range(_BACKOFF_AFTER_EMPTY):
        xhs.search_xhs(f'legacy empty {index}')
    assert capture_source(auth_source_scope.current_binding(), 'xiaohongshu.com').guard.in_cooldown()
    native = NativeProvider([{'title': 'Owned result', 'url': 'https://example.test/native'}])
    native.search = Mock(wraps=native.search)
    providers.register_site_search_provider(native)
    assert xhs.search_xhs('native query')[0]['title'] == 'Owned result'
    native.search.assert_called_once()


def test_native_outcomes_never_poison_legacy_cache_or_backoff(monkeypatch, rig):
    monkeypatch.setattr(xhs, 'get_config', lambda: SearchConfig(
        xhs_min_interval_s=0, xhs_cache_ttl_s=600, xhs_backoff_cooldown_s=600))
    native = NativeProvider([{'title': 'Native', 'url': 'https://example.test/native'}])
    providers.register_site_search_provider(native)
    assert xhs.search_xhs('same query')
    native.outcome = []
    for index in range(_BACKOFF_AFTER_EMPTY):
        assert xhs.search_xhs(f'empty {index}') == []
    assert not capture_source(auth_source_scope.current_binding(), 'xiaohongshu.com').guard.in_cooldown()
    providers.register_site_search_provider(None)
    rig[1].return_value = [{'title': 'Legacy', 'url': 'https://example.test/legacy'}]
    assert xhs.search_xhs('same query')[0]['title'] == 'Legacy'
    rig[1].assert_called_once()


@pytest.mark.parametrize('pool_available', [True, False])
def test_replay_first_policy_preserves_native_fallback_ownership(monkeypatch, rig, pool_available):
    providers.get_auth_source_provider().get_source.return_value['access_strategy'] = 'cookies_replay'
    monkeypatch.setattr(xhs, 'get_config', lambda: SearchConfig(
        xhs_min_interval_s=0, xhs_cache_ttl_s=600, xhs_backoff_cooldown_s=600))
    native = NativeProvider([{'title': 'Native', 'url': 'https://example.test/native'}])
    native.search = Mock(wraps=native.search)
    providers.register_site_search_provider(native)
    rig[1].return_value = (
        [{'title': 'Replay', 'url': 'https://example.test/replay'}]
        if pool_available else None)
    for _ in range(2):
        assert xhs.search_xhs('same query')[0]['title'] == (
            'Replay' if pool_available else 'Native')
    if pool_available:
        rig[1].assert_called_once()
        native.search.assert_not_called()
    else:
        assert rig[1].call_count == native.search.call_count == 2
    rig[0].scrape.assert_not_called()


def test_concurrent_public_searches_keep_native_binding_and_skip_legacy_state(monkeypatch, rig):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import ContextVar
    from threading import Barrier

    owner = ContextVar('fixture_native_owner')
    together = Barrier(2)

    class Bound(NativeProvider):
        def search(self, source_id, query, **kwargs):
            together.wait(timeout=10)
            return self.outcome

    class Template(NativeProvider):
        def bind(self):
            return Bound([{
                'title': owner.get(), 'url': 'https://example.test/owned',
                'snippet': f'Same query catalogue for {owner.get()} includes owned records. ' * 5,
            }])

    providers.register_site_search_provider(Template(None))
    forbidden = Mock(side_effect=AssertionError('native execution touched legacy account state'))
    for name in ('in_cooldown', 'cached_results', 'wait_turn', 'report_outcome'):
        monkeypatch.setattr(RiskGuard, name, forbidden)

    def run(identity):
        token = owner.set(identity)
        try:
            return orchestrator.perform_web_search(
                'same query', engines=['Xiaohongshu'], fetch_pages=False,
                filter_pages=False, rerank=False)
        finally:
            owner.reset(token)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run, ['first', 'second']))
    assert [[row['title'] for row in rows] for rows in results] == [['first'], ['second']]
    forbidden.assert_not_called()

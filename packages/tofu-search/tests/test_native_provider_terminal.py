"""Native failures are authoritative, not permission for cookie replay."""

from unittest.mock import Mock

import pytest

import tofu_search
from tofu_search import providers
from tofu_search.config import SearchConfig
from tofu_search.search import orchestrator
from tofu_search.search.engines import xhs


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
    auth.get_source.return_value = {
        'enabled': True, 'cookies': [{'name': 'session', 'value': 'fake'}],
        'access_strategy': 'browser_first',
    }
    pool = Mock(return_value=[])
    monkeypatch.setattr(_pw_pool, 'search_authenticated', pool)
    monkeypatch.setattr(providers, '_browser_provider', browser)
    monkeypatch.setattr(providers, '_auth_source_provider', auth)
    monkeypatch.setattr(providers, '_site_search_provider', None)
    monkeypatch.setattr(xhs, '_GUARD', xhs._RiskGuard())
    monkeypatch.setattr(xhs, 'get_config', lambda: SearchConfig(
        xhs_min_interval_s=0, xhs_cache_ttl_s=0, xhs_backoff_cooldown_s=0))
    return browser, pool


def test_public_terminal_error_stops_compat_and_pool(monkeypatch, rig):
    assert tofu_search.SiteSearchTerminalError is providers.SiteSearchTerminalError
    error = tofu_search.SiteSearchTerminalError()
    monkeypatch.setattr(providers, '_site_search_provider', NativeProvider(error))
    assert xhs.xhs_search_available()
    with pytest.raises(tofu_search.SiteSearchTerminalError) as caught:
        xhs.search_xhs('private query')
    assert caught.value is error
    assert 'private query' not in str(error)
    rig[0].scrape.assert_not_called()
    rig[1].assert_not_called()


@pytest.mark.parametrize('engine', ['Xiaohongshu', 'Site:catalog'])
def test_explicit_orchestrator_reports_terminal_engine_error(monkeypatch, rig, engine):
    monkeypatch.setattr(providers, '_site_search_provider', NativeProvider(
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
    monkeypatch.setattr(providers, '_site_search_provider', NativeProvider([]))
    assert xhs.search_xhs('query') == []
    rig[0].scrape.assert_not_called()
    rig[1].assert_not_called()


@pytest.mark.parametrize('outcome', [None, RuntimeError('legacy unavailable')])
def test_unavailable_and_legacy_errors_keep_fallback(monkeypatch, rig, outcome):
    monkeypatch.setattr(providers, '_site_search_provider', NativeProvider(outcome))
    assert xhs.search_xhs('query') == []
    rig[0].scrape.assert_called_once()
    rig[1].assert_called_once()


def test_standalone_unavailable_stays_inert(monkeypatch, rig):
    monkeypatch.setattr(providers, '_browser_provider', None)
    monkeypatch.setattr(providers, '_auth_source_provider', None)
    assert not xhs.xhs_search_available()
    assert xhs.search_xhs('query') == []
    rig[1].assert_not_called()

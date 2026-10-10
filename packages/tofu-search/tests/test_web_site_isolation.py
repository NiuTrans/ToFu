"""Offline contracts: native site capabilities never join unscoped Web search."""

from unittest.mock import Mock

import pytest

from tofu_search import providers
from tofu_search.search import orchestrator as orch
from tofu_search.search.engines import xhs

pytestmark = pytest.mark.unit


@pytest.fixture
def rig(monkeypatch):
    site = Mock(spec=providers.SiteSearchProvider)
    site.bind.return_value = site
    site.list_sources.return_value = [
        {'id': 'catalog', 'domains': ['catalog.example']},
        {'id': 'xiaohongshu', 'aliases': ['xhs']},
    ]
    site.search.return_value = []
    monkeypatch.setattr(providers, '_site_search_provider', site)
    browser = Mock(spec=providers.BrowserProvider)
    browser.bind.return_value = browser
    monkeypatch.setattr(providers, '_browser_provider', browser)
    mocks = {}
    for name in ('search_ddg_html', 'search_ddg_api', 'search_brave',
                 'search_bing', 'search_searxng', 'search_marginalia',
                 'search_via_browser', 'search_xhs', 'xhs_search_available',
                 'fetch_page_content'):
        mocks[name] = Mock(return_value=[])
        monkeypatch.setattr(orch, name, mocks[name])
    return site, browser, mocks


def search(query='model', **kwargs):
    return orch.perform_web_search(query, fetch_pages=False, filter_pages=False,
                                   rerank=False, deepen=False, **kwargs)


def test_default_only_web_without_site_discovery(rig):
    site, browser, mocks = rig
    mocks['search_bing'].return_value = [
        {'url': 'https://example.com/docs', 'title': 'Official docs'}]
    assert search('site:example.com official docs')
    site.list_sources.assert_not_called()
    site.search.assert_not_called()
    mocks['xhs_search_available'].assert_not_called()
    mocks['search_xhs'].assert_not_called()
    browser.scrape.assert_not_called()
    browser.search.assert_not_called()
    browser.fetch_html.assert_not_called()


@pytest.mark.parametrize('engines', [[], ['unknown'], ['Site:missing'],
                                    ['Xiaohongshu'], ['Bing', 'unknown']])
def test_explicit_invalid_selection_never_widens(rig, engines):
    site, browser, mocks = rig
    result = search(engines=engines)
    assert result == []
    assert result._search_diag['reason'] == 'invalid_engines'
    site.search.assert_not_called()
    for name, fn in mocks.items():
        if name != 'xhs_search_available':
            fn.assert_not_called()
    browser.scrape.assert_not_called()


@pytest.mark.parametrize('response', [[], None])
def test_native_empty_or_unavailable_never_widens(rig, response):
    site, browser, mocks = rig
    site.search.return_value = response
    result = search(engines=['Site:catalog'])
    assert result == []
    assert result._search_diag
    site.search.assert_called_once_with('catalog', 'model', max_results=10, freshness='')
    for fn in mocks.values():
        fn.assert_not_called()
    browser.scrape.assert_not_called()


@pytest.mark.parametrize('engine', ['Site:catalog', 'Xiaohongshu'])
@pytest.mark.parametrize('query,freshness', [
    ('site:other.example model', ''), ('site:catalog.example model', ''),
    ('-SITE:catalog.example model', ''), ('filetype:pdf model', ''),
    ('intitle:model', ''), ('model after:2025-01-01', ''), ('model', 'week'),
])
def test_native_constraints_rejected_before_discovery_or_navigation(rig, engine, query, freshness):
    site, browser, mocks = rig
    result = search(query, engines=[engine], freshness=freshness)
    assert result._search_diag['reason'] == 'unsupported_site_query'
    assert 'cannot enforce' in result._search_diag['reason_detail']
    site.list_sources.assert_not_called()
    site.search.assert_not_called()
    for fn in mocks.values():
        fn.assert_not_called()
    browser.scrape.assert_not_called()


def test_explicit_xhs_uses_provider_and_keeps_guard_cache(rig, monkeypatch):
    site, browser, mocks = rig
    monkeypatch.setattr(orch, 'search_xhs', xhs.search_xhs)
    monkeypatch.setattr(orch, 'xhs_search_available', xhs.xhs_search_available)
    monkeypatch.setattr(xhs, '_GUARD', xhs._RiskGuard())
    monkeypatch.setattr(xhs, '_get_source', lambda: None)
    site.search.return_value = [
        {'title': 'Model', 'url': 'https://www.xiaohongshu.com/explore/model'}]
    assert search(engines=['Xiaohongshu'])
    assert search(engines=['Xiaohongshu'])
    site.search.assert_called_once_with('xiaohongshu', 'model', max_results=10)
    browser.scrape.assert_not_called()
    mocks['search_ddg_html'].assert_not_called()
    mocks['search_brave'].assert_not_called()
    mocks['search_via_browser'].assert_not_called()


def test_explicit_web_selection_does_not_widen(rig):
    site, browser, mocks = rig
    assert search(engines=['Bing']) == []
    mocks['search_bing'].assert_called_once()
    mocks['search_ddg_html'].assert_not_called()
    mocks['search_via_browser'].assert_not_called()
    site.list_sources.assert_not_called()

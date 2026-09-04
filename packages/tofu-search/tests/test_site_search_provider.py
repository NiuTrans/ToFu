"""Optional read-only site-search provider and browser/server trust split."""

from __future__ import annotations

import threading

import pytest

import tofu_search
from tofu_search.fetch import core as fetch_core
from tofu_search.search.orchestrator import perform_web_search

pytestmark = pytest.mark.unit


class _Catalog(tofu_search.SiteSearchProvider):
    def __init__(self):
        self.calls = []

    def list_sources(self):
        return [{'id': 'catalog', 'name': 'Internal Catalog'}]

    def search(self, source_id, query, *, max_results=10, freshness=''):
        self.calls.append((source_id, query, max_results, freshness))
        return [
            {'title': 'Model A', 'url': 'https://catalog.example/model/a',
             'snippet': 'short'},
            {'title': 'Model A duplicate',
             'url': 'https://catalog.example/model/a?utm_source=test',
             'snippet': 'a longer description'},
            {'title': '', 'url': 'javascript:alert(1)'},
        ]


def test_provider_is_optional_and_normalization_keeps_wire_shape():
    assert tofu_search.get_site_search_provider() is None
    assert tofu_search.normalize_site_search_results(None, source_id='x') == []
    rows = tofu_search.normalize_site_search_results(
        [{'title': ' T ', 'url': 'https://example.com/x', 'extra': 7}],
        source_id='catalog', source_name='Catalog')
    assert rows == [{
        'title': 'T', 'url': 'https://example.com/x', 'extra': 7,
        'snippet': '', 'source': 'Catalog',
        'metadata': {'site_source': 'catalog'},
    }]


def test_site_results_join_the_normal_pipeline_and_deduplicate_urls():
    provider = _Catalog()
    tofu_search.register_site_search_provider(provider)
    rows = perform_web_search(
        'model', max_results=5, engines=['Site:catalog'],
        fetch_pages=False, filter_pages=False, rerank=False, deepen=False)
    assert len(rows) == 1
    assert rows[0]['url'] == 'https://catalog.example/model/a'
    assert rows[0]['snippet'] == 'a longer description'
    assert rows[0]['metadata']['site_source'] == 'catalog'
    assert provider.calls == [('catalog', 'model', 10, '')]


def test_bound_provider_context_reaches_engine_worker_threads():
    seen = []

    class Bound(tofu_search.SiteSearchProvider):
        def list_sources(self):
            return [{'id': 'bound'}]

        def search(self, source_id, query, *, max_results=10, freshness=''):
            seen.append((source_id, threading.current_thread().name))
            return [{'title': 'Bound result', 'url': 'https://example.com/bound'}]

    class NeedsBinding(tofu_search.SiteSearchProvider):
        def bind(self):
            seen.append(('bind', threading.current_thread().name))
            return Bound()

        def list_sources(self):
            raise AssertionError('unbound provider must never be used')

    tofu_search.register_site_search_provider(NeedsBinding())
    rows = perform_web_search(
        'bound', max_results=2, engines=['Site:bound'],
        fetch_pages=False, filter_pages=False, rerank=False, deepen=False)
    assert [row['title'] for row in rows] == ['Bound result']
    assert seen[0][0] == 'bind'
    assert seen[1][0] == 'bound'
    assert seen[0][1] != seen[1][1], 'search must exercise executor propagation'


def test_ssrf_refusal_can_use_browser_but_other_server_gates_stay_closed(monkeypatch):
    calls = []

    def browser(url, max_chars, reason=''):
        calls.append(reason)
        return 'content from the user browser'

    def refused(reason):
        def gate(url, diag=None):
            if diag is not None:
                diag.update(reason=reason, detail='blocked')
            return False
        return gate

    monkeypatch.setattr(fetch_core, '_try_browser_fetch', browser)
    monkeypatch.setattr(fetch_core, '_should_fetch', refused('ssrf_blocked'))
    assert fetch_core.fetch_page_content('https://intranet.example/page') \
        == 'content from the user browser'
    assert calls == ['server_ssrf_browser_bypass']

    calls.clear()
    monkeypatch.setattr(fetch_core, '_should_fetch', refused('skip_domain'))
    result = fetch_core.fetch_page_content('https://blocked.example/page')
    assert result is None
    assert calls == [], 'explicit server gates must not become browser bypasses'

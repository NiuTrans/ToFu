"""Failure-injected search observations preserve relevance and scope truth."""

import pytest

from tofu_search.config import get_config
from tofu_search.fetch.html_extract import extract_html_text
from tofu_search.search import orchestrator
from tofu_search.search.format import format_search_for_tool_response
from tofu_search.search.quality import matches_site_constraints, site_scopes

pytestmark = pytest.mark.unit


def candidate(url, title='quasar context', content=''):
    return {'url': url, 'title': title, 'snippet': title, 'source': 'Bing', 'full_content': content}


@pytest.mark.parametrize('query,url,accepted', [
    ('site:vendor.test quasar', 'https://docs.vendor.test/a', True),
    ('site:vendor.test quasar', 'https://vendor.test.evil.test/a', False),
    ('site:vendor.test quasar', 'https://evil.test/vendor.test', False),
    ('site:vendor.test/docs quasar', 'https://vendor.test/docs/a', True),
    ('site:vendor.test/docs quasar', 'https://vendor.test/docs-evil/a', False),
    ('site:a.test OR site:b.test quasar', 'https://b.test/a', True),
    ('site:a.test -site:private.a.test quasar', 'https://private.a.test/a', False),
])
def test_site_constraints(query, url, accepted):
    assert matches_site_constraints(url, site_scopes(query)) is accepted


def controlled_search(monkeypatch, results, query, *, fetch_pages=False):
    for name in ('search_ddg_html', 'search_brave', 'search_bing', 'search_ddg_api',
                 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orchestrator, name, lambda *a, **kw: [])
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a, **kw: results)
    def unavailable(*a, **kw):
        raise OSError('Injected engine outage')
    monkeypatch.setattr(orchestrator, 'search_brave', unavailable)
    monkeypatch.setattr(orchestrator, 'search_via_browser', lambda *a, **kw: [])
    return orchestrator.perform_web_search(query, fetch_pages=fetch_pages,
        filter_pages=False, config=get_config().copy(search_deadline_secs=5), deepen=False)


def test_wrong_site_is_never_fetched_even_at_recall_floor(monkeypatch):
    fetched = []
    monkeypatch.setattr(orchestrator, 'fetch_page_content', lambda url, **kw: fetched.append(url))
    result = controlled_search(monkeypatch, [candidate('https://junk.test/a')],
                               'site:vendor.test quasar', fetch_pages=True)
    assert result == [] and fetched == []
    assert result._search_diag['constraint_rejected'] == 1
    assert result._search_diag['reason'] == 'no_usable_evidence'
    rendered = format_search_for_tool_response(result)
    assert 'does not establish' in rendered and 'junk.test' not in rendered


def test_unrelated_snippets_are_candidates_not_search_evidence(monkeypatch):
    result = controlled_search(monkeypatch, [candidate('https://junk.test/a', 'Garden tomatoes')], 'quasar latency')
    assert result == []
    assert result._search_diag['unverified_count'] == 1
    rendered = format_search_for_tool_response(result)
    assert 'Candidate for follow-up: https://junk.test/a' in rendered
    assert 'not evidence of relevance or absence' in rendered


def test_degradation_is_visible_even_when_search_has_results(monkeypatch):
    result = controlled_search(monkeypatch, [candidate('https://vendor.test/a')], 'quasar context')
    assert len(result) == 1
    assert 'Brave' in result._search_diag['engine_errors']
    rendered = format_search_for_tool_response(result)
    assert 'failed=Brave' in rendered and 'Coverage is not exhaustive' in rendered
    assert 'lexical_match' in rendered


def test_cross_language_candidate_with_fetched_evidence_is_retained(monkeypatch):
    monkeypatch.setattr(orchestrator, 'fetch_page_content', lambda *a, **kw: '模型参数：上下文一百万' * 10)
    result = controlled_search(monkeypatch, [candidate('https://vendor.test/a', '模型参数')],
                               'quasar context', fetch_pages=True)
    assert len(result) == 1
    assert result[0]['full_content']
    assert result[0]['relevance'] == 'unverified'


def test_article_extractor_cannot_silently_erase_tables_and_copy_identifiers(monkeypatch):
    monkeypatch.setattr('tofu_search.fetch.html_extract.trafilatura.extract', lambda *a, **kw: 'Main documentation overview')
    html = '<main><p>Main documentation overview and documentation introduction.</p><button>quasar-x-preview</button><table><tr><th>Model</th><th>Context</th></tr><tr><td>Quasar X</td><td>1,000,000</td></tr></table></main>'
    text = extract_html_text(html, max_chars=0, url='https://vendor.test/docs')
    assert '| Quasar X | 1,000,000 |' in text
    assert 'quasar-x-preview' in text
    assert text.startswith('Main documentation overview')


def test_circuit_open_is_unavailable_not_a_genuine_empty_result(monkeypatch):
    from tofu_search.search._common import EngineUnavailable
    for name in ('search_ddg_html', 'search_brave', 'search_bing', 'search_ddg_api',
                 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orchestrator, name, lambda *a, **kw: EngineUnavailable('circuit_open'))
    monkeypatch.setattr(orchestrator, 'search_via_browser', lambda *a, **kw: [])
    result = orchestrator.perform_web_search('quasar', fetch_pages=False,
        config=get_config().copy(search_deadline_secs=5))
    assert result == []
    assert result._search_diag['engine_empty'] == []
    assert len(result._search_diag['engine_unavailable']) == 6
    assert 'unavailable=' in format_search_for_tool_response(result)



def test_branch_local_or_scope_and_quoted_literal_are_not_global_constraints():
    assert site_scopes('site:a.test quasar OR unrelated') == []
    assert site_scopes('"description mentioning site:a.test"') == []
    assert site_scopes('site:a.test (quasar OR nebula)') == [(False, 'a.test')]

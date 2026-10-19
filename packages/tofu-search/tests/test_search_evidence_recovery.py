"""Automatic useful-evidence recovery never relaxes query authority."""

import pytest

from tofu_search.config import get_config
from tofu_search.search import orchestrator

pytestmark = pytest.mark.unit


def row(url, title, content=''):
    return {'url': url, 'title': title, 'snippet': title,
            'source': 'Bing', 'full_content': content}


def engines_off(monkeypatch):
    for name in ('search_ddg_html', 'search_brave', 'search_bing',
                 'search_ddg_api', 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orchestrator, name, lambda *a, **kw: [])
    monkeypatch.setattr(orchestrator, 'search_via_browser', lambda *a, **kw: [])
    monkeypatch.setattr(orchestrator.time, 'sleep', lambda secs: None)


def test_first_token_junk_does_not_block_original_query_recovery(monkeypatch):
    engines_off(monkeypatch)
    query = 'github brave search MCP server implementation'
    junk = row('https://github.com/', 'GitHub homepage')
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a: [junk])
    calls = []
    useful = row('https://github.com/brave/search-mcp', 'Brave search MCP server implementation')

    def ddg(q, n, freshness):
        calls.append((q, freshness))
        return [] if len(calls) == 1 else [useful]

    monkeypatch.setattr(orchestrator, 'search_ddg_html', ddg)
    result = orchestrator.perform_web_search(query, freshness='month', fetch_pages=False,
        filter_pages=False, deepen=False, config=get_config().copy(search_deadline_secs=45))
    assert [r['url'] for r in result] == [useful['url']]
    assert calls == [(query, 'month'), (query, 'month')]
    assert result._search_diag['weak_match_count'] == 1
    assert result._search_diag['fallback_attempts'] == ['DDG-HTML']
    assert 'relevance' not in junk  # engine-owned observations are never mutated


def test_weak_nonempty_ddg_retry_still_allows_brave_recovery(monkeypatch):
    engines_off(monkeypatch)
    query = 'quasar latency benchmark results'
    weak = row('https://junk.test/', 'Quasar homepage')
    useful = row('https://vendor.test/benchmark', 'Quasar latency benchmark results')
    monkeypatch.setattr(orchestrator, 'search_ddg_html', lambda *a: [weak])
    calls = []

    def brave(q, n, freshness):
        calls.append((q, freshness))
        return [] if len(calls) == 1 else [useful]

    monkeypatch.setattr(orchestrator, 'search_brave', brave)
    result = orchestrator.perform_web_search(query, fetch_pages=False, filter_pages=False,
        deepen=False, config=get_config().copy(search_deadline_secs=45))
    assert [r['url'] for r in result] == [useful['url']]
    assert result._search_diag['fallback_attempts'] == ['DDG-HTML', 'Brave']


def test_explicit_engine_selection_never_recovers_through_another_engine(monkeypatch):
    engines_off(monkeypatch)
    monkeypatch.setattr(orchestrator, 'search_bing',
        lambda *a: [row('https://junk.test/', 'GitHub homepage')])
    def forbidden(*a, **kw):
        pytest.fail('Explicit selection must not call another provider')
    monkeypatch.setattr(orchestrator, 'search_ddg_html', forbidden)
    result = orchestrator.perform_web_search('github brave MCP search server',
        engines=['Bing'], fetch_pages=False, filter_pages=False, deepen=False,
        config=get_config().copy(search_deadline_secs=45))
    assert result == []
    assert result._search_diag['fallback_attempts'] == []
    assert result._search_diag['reason'] == 'no_usable_evidence'


def test_successful_fetch_does_not_upgrade_a_generic_word_match(monkeypatch):
    engines_off(monkeypatch)
    monkeypatch.setattr(orchestrator, 'search_bing',
        lambda *a: [row('https://github.com/', 'GitHub homepage')])
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
        lambda *a, **kw: 'GitHub collaboration home for teams. ' * 30)
    result = orchestrator.perform_web_search('github brave MCP search server',
        engines=['Bing'], filter_pages=False, deepen=False,
        config=get_config().copy(search_deadline_secs=5))
    assert result == []
    assert result._unverified_candidates[0]['full_content']
    assert result._search_diag['weak_match_count'] == 1


def test_final_deep_admission_rechecks_original_site_scope(monkeypatch):
    engines_off(monkeypatch)
    monkeypatch.setattr(orchestrator, 'search_bing',
        lambda *a: [row('https://vendor.test/docs', 'S5M2 Firmware')])
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
        lambda *a, **kw: 'S5M2 Firmware details and version history. ' * 20)
    monkeypatch.setattr(orchestrator, 'deepen_results',
        lambda *a, **kw: [row('https://evil.test/firmware', 'S5M2 Firmware',
                             'S5M2 Firmware details. ' * 20)])
    result = orchestrator.perform_web_search('site:vendor.test "S5M2" "Firmware"',
        engines=['Bing'], filter_pages=False, deepen=True,
        config=get_config().copy(search_deadline_secs=5))
    assert [r['url'] for r in result] == ['https://vendor.test/docs']
    assert result._search_diag['constraint_rejected'] == 1


def test_freshness_does_not_disappear_in_browser_recovery(monkeypatch):
    engines_off(monkeypatch)
    def forbidden(*a, **kw):
        pytest.fail('Legacy browser search cannot enforce freshness')
    monkeypatch.setattr(orchestrator, 'search_via_browser', forbidden)
    result = orchestrator.perform_web_search('quasar benchmark', freshness='week',
        fetch_pages=False, filter_pages=False, deepen=False,
        config=get_config().copy(search_deadline_secs=45))
    assert result == []
    assert 'Browser' not in result._search_diag['fallback_attempts']


def test_generic_firmware_page_is_uncertain_for_named_camera():
    from tofu_search.search.quality import annotate_relevance
    generic = row('https://vendor.test/firmware', 'Firmware download page')
    annotate_relevance(generic, 'site:vendor.test "S5M2" "Firmware"')
    assert generic['relevance'] == 'weak_match'
    specific = row('https://vendor.test/s5m2', 'DC-S5M2 Firmware download')
    annotate_relevance(specific, 'site:vendor.test "S5M2" "Firmware"')
    assert specific['relevance'] == 'lexical_match'


def test_user_supplied_lumix_research_is_relevant_but_not_panasonic_site():
    from tofu_search.search.quality import annotate_relevance, matches_site_constraints, site_scopes
    # Short manually authored fixture grounded in the user-provided README;
    # this tests relevance, not the repository's technical claims or discovery.
    research = row('https://github.com/Siegfried82/lumix-s1m2-research',
        'S5 / S5M2 固件研究交接报告',
        'Panasonic LUMIX S5 / S5M2 固件研究。提取不是解密；尚无可信程序明文。')
    annotate_relevance(research, '"S5M2" firmware decrypt')
    assert research['relevance'] == 'lexical_match'
    assert not matches_site_constraints(research['url'], site_scopes(
        'site:av.jpn.support.panasonic.com "S5M2" "Firmware"'))


def test_generic_acronym_does_not_suppress_evidence_recovery():
    from tofu_search.search.quality import annotate_relevance
    wrong = row('https://weather.test/api', 'Weather API documentation')
    annotate_relevance(wrong, 'Brave Search API extra snippets')
    assert wrong['relevance'] == 'weak_match'


@pytest.mark.parametrize('body', [
    '模型参数：上下文长度一百万。' * 10,
    'quasar 模型参数：上下文长度一百万。' * 10,
])
def test_multilingual_partial_overlap_never_penalizes_recall(monkeypatch, body):
    engines_off(monkeypatch)
    monkeypatch.setattr(orchestrator, 'search_bing',
        lambda *a: [row('https://vendor.test/zh', '模型参数')])
    monkeypatch.setattr(orchestrator, 'fetch_page_content', lambda *a, **kw: body)
    result = orchestrator.perform_web_search('quasar context length',
        engines=['Bing'], filter_pages=False, deepen=False,
        config=get_config().copy(search_deadline_secs=5))
    assert len(result) == 1
    assert result[0]['relevance'] in ('weak_match', 'unverified')
    assert result[0]['relevance_evidence']['cross_language']


def test_fetch_success_without_subject_evidence_is_retained_only_as_candidate(monkeypatch):
    engines_off(monkeypatch)
    monkeypatch.setattr(orchestrator, 'search_bing',
        lambda *a: [row('https://junk.test/garden', 'Garden tomatoes')])
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
        lambda *a, **kw: 'Garden tomatoes planting advice. ' * 20)
    result = orchestrator.perform_web_search('quasar context length',
        engines=['Bing'], filter_pages=False, deepen=False,
        config=get_config().copy(search_deadline_secs=5))
    assert result == []
    assert result._unverified_candidates[0]['full_content']
    assert result._search_diag['reason'] == 'no_usable_evidence'


def test_generic_partial_matches_do_not_monopolize_fetch_budget(monkeypatch):
    engines_off(monkeypatch)
    rows = [row(f'https://host{i}.test/', f'GitHub homepage unique{i}alpha unique{i}beta unique{i}gamma')
            for i in range(20)]
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *a: rows)
    fetched = []
    def fetch(url, **kw):
        fetched.append(url)
        return 'GitHub collaboration homepage. ' * 20
    monkeypatch.setattr(orchestrator, 'fetch_page_content', fetch)
    result = orchestrator.perform_web_search('github brave search MCP server implementation',
        engines=['Bing'], max_results=10, filter_pages=False, deepen=False,
        config=get_config().copy(search_deadline_secs=5))
    assert result == []
    assert len(fetched) <= get_config().prefetch_gate_min_fetch
    assert result._search_diag['weak_match_count'] > 0

"""Tests for the pre-fetch relevance gate (search/prefetch_gate.py) and its
integration into the orchestrator's fetch-submission path.

The gate's job: decline to FETCH results that share zero query terms with a
substantive query (off-topic SERP junk), while staying fail-open for short
queries and the leading recall floor. Skipped results are still kept as
snippet-only candidates by the orchestrator.
"""

import pytest

from tofu_search.config import get_config
from tofu_search.search import orchestrator
from tofu_search.search.orchestrator import perform_web_search
from tofu_search.search.prefetch_gate import (
    partition_fetchable,
    query_terms,
    should_fetch_result,
)

# ── Unit tests for the gate itself ──

def test_query_terms_strips_stopwords():
    terms = query_terms('the R2E-Gym hybrid verifiers for SWE agents')
    # stop words ('the', 'for') dropped; 'r2e'/'gym' split on hyphen
    assert 'verifiers' in terms
    assert 'hybrid' in terms
    assert 'the' not in terms
    assert 'for' not in terms


def test_should_fetch_zero_overlap_false():
    q = query_terms('R2E-Gym hybrid verifiers SWE agents')
    junk = {'title': 'HPV infection - Symptoms and causes',
            'snippet': 'Mayo Clinic overview of cervical cancer risk.'}
    assert should_fetch_result(junk, q) is False


def test_should_fetch_any_overlap_true():
    q = query_terms('R2E-Gym hybrid verifiers SWE agents')
    ontopic = {'title': 'R2E-Gym: procedural environments and hybrid verifiers',
               'snippet': 'Training SWE agents with execution-based verifiers.'}
    assert should_fetch_result(ontopic, q) is True


def test_should_fetch_failopen_no_query_terms():
    # No meaningful query terms → always fetch.
    assert should_fetch_result({'title': 'anything', 'snippet': ''}, set()) is True


def test_partition_short_query_is_noop():
    # 1 meaningful term < min_query_terms=2 → fetch everything.
    results = [{'title': 'cats', 'snippet': 'feline', 'url': 'u1'},
               {'title': 'dogs', 'snippet': 'canine', 'url': 'u2'}]
    to_fetch, skipped = partition_fetchable('numpy', results)
    assert len(to_fetch) == 2
    assert skipped == []


def test_partition_recall_floor_admits_leading():
    q = 'R2E-Gym hybrid verifiers SWE agents'
    # All three are off-topic, but min_fetch=3 admits the leading 3 anyway.
    junk = [{'title': 'HPV infection', 'snippet': 'mayo clinic', 'url': f'u{i}'}
            for i in range(5)]
    to_fetch, skipped = partition_fetchable(q, junk, min_fetch=3)
    assert len(to_fetch) == 3
    assert len(skipped) == 2


def test_partition_keeps_relevant_drops_offtopic():
    q = 'R2E-Gym hybrid verifiers SWE agents'
    results = [
        {'title': 'R2E-Gym hybrid verifiers', 'snippet': 'SWE agents', 'url': 'good1'},
        {'title': 'unrelated', 'snippet': 'nothing matches', 'url': 'mid'},
        {'title': 'verifiers for agents', 'snippet': 'SWE', 'url': 'good2'},
        {'title': 'HPV infection symptoms', 'snippet': 'cervical cancer mayo', 'url': 'junk'},
    ]
    # min_fetch=0 so the recall floor doesn't mask the gate decision.
    to_fetch, skipped = partition_fetchable(q, results, min_fetch=0)
    urls_fetch = {r['url'] for r in to_fetch}
    urls_skip = {r['url'] for r in skipped}
    assert 'good1' in urls_fetch and 'good2' in urls_fetch
    assert 'junk' in urls_skip
    assert 'mid' in urls_skip  # zero overlap too


# ── Integration: orchestrator skips the fetch but keeps the candidate ──

@pytest.fixture
def stub_engines(monkeypatch):
    """One engine returns 2 on-topic + 3 off-topic results; fetch stubbed."""
    def _engine(query, n=6, freshness=''):
        return [
            {'title': 'verifiers hybrid', 'snippet': 'agents swe gym',
             'url': 'https://ok.com/1', 'source': 'ddg'},
            {'title': 'hybrid verifiers benchmark', 'snippet': 'swe agents',
             'url': 'https://ok.com/2', 'source': 'ddg'},
            {'title': 'HPV infection', 'snippet': 'cervical cancer mayo clinic',
             'url': 'https://junk.com/a', 'source': 'ddg'},
            {'title': 'throat cancer', 'snippet': 'symptoms causes',
             'url': 'https://junk.com/b', 'source': 'ddg'},
            {'title': 'endometrial cancer', 'snippet': 'diagnosis treatment',
             'url': 'https://junk.com/c', 'source': 'ddg'},
        ]

    monkeypatch.setattr(orchestrator, 'search_ddg_html', _engine)
    for name in ('search_brave', 'search_bing', 'search_ddg_api',
                 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orchestrator, name, lambda q, n=6, freshness='': [])
    monkeypatch.setattr(orchestrator, 'xhs_search_available', lambda: False)
    monkeypatch.setattr(orchestrator, 'is_deepen_enabled', lambda: False)

    fetched = []

    def _fetch(url, **kw):
        fetched.append(url)
        return 'x' * 500

    monkeypatch.setattr(orchestrator, 'fetch_page_content', _fetch)
    return fetched


def test_orchestrator_does_not_fetch_offtopic(stub_engines):
    fetched = stub_engines
    # min_fetch default is 3 → admits 2 on-topic + 1 junk; skips 2 junk.
    cfg = get_config().copy(prefetch_gate_min_fetch=2)
    out = perform_web_search('hybrid verifiers swe agents gym', max_results=6,
                             filter_pages=False, rerank=False, config=cfg)
    fetched_set = set(fetched)
    # The two on-topic pages were fetched.
    assert 'https://ok.com/1' in fetched_set
    assert 'https://ok.com/2' in fetched_set
    # At least one off-topic junk page was NOT fetched.
    assert any(u not in fetched_set for u in
               ('https://junk.com/a', 'https://junk.com/b', 'https://junk.com/c'))
    # But skipped junk is still RETURNED as a snippet-only candidate (not dropped).
    out_urls = {r['url'] for r in out}
    assert 'https://junk.com/c' in out_urls or 'https://junk.com/b' in out_urls


def test_orchestrator_gate_disabled_fetches_all(stub_engines):
    fetched = stub_engines
    cfg = get_config().copy(prefetch_gate_enabled=False)
    perform_web_search('hybrid verifiers swe agents gym', max_results=6,
                       filter_pages=False, rerank=False, config=cfg)
    # Gate off → every URL fetched.
    assert len(set(fetched)) == 5

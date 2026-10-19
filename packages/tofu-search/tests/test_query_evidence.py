"""Executable lexical evidence contract; fixtures model reported SERP junk."""

import pytest

from tofu_search.search.prefetch_gate import partition_fetchable, query_terms, result_terms
from tofu_search.search.quality import annotate_relevance, classify_relevance, fetched_body

pytestmark = pytest.mark.unit

MCP_QUERY = 'github brave search MCP server extra_snippets summary search results'


@pytest.mark.parametrize('title,snippet,status', [
    ('GitHub', 'Build and ship software on GitHub', 'weak_match'),
    ('GitHub search results', 'Search repositories and documentation', 'weak_match'),
    ('brave-search MCP server', 'Returns extra_snippets and summaries', 'lexical_match'),
    ('Brave Search API', 'Get search results with additional snippets', 'lexical_match'),
    ('HPV infection - Symptoms and causes', 'Mayo Clinic cancer risk', 'unverified'),
    ('Throat cancer', 'Symptoms, diagnosis and treatment', 'unverified'),
])
def test_positive_and_junk_serp_fixtures(title, snippet, status):
    result = {'title': title, 'snippet': snippet}
    assert annotate_relevance(result, MCP_QUERY) is result
    assert result['relevance'] == status
    assert result['relevance_evidence']['matched_terms'] == sorted(
        query_terms(MCP_QUERY) & result_terms(result))


def test_fetch_does_not_upgrade_github_homepage():
    result = {'title': 'GitHub', 'full_content': 'Build and ship software on GitHub'}
    assert classify_relevance(result, MCP_QUERY)['status'] == 'weak_match'


def test_body_late_evidence_not_truncated_or_confused_with_links():
    result = {'title': 'Documentation', 'full_content': 'Introduction. ' * 2000 +
              '\nextra snippets from Brave search\n\n--- Page Links ---\n- [Unrelated](https://example.test)'}
    evidence = classify_relevance(result, MCP_QUERY)
    assert evidence['status'] == 'lexical_match'
    assert evidence['sources']['body']['status'] == 'lexical_match'
    assert evidence['sources']['serp']['status'] == 'unverified'
    assert evidence['sources']['body']['matched_terms'] == ['brave', 'extra', 'search', 'snippets']
    assert evidence['links']['matched_terms'] == []
    assert 'Page Links' not in fetched_body(result)


def test_link_inventory_cannot_prove_body_subject():
    result = {'title': 'Navigation', 'full_content': 'General landing page.\n\n'
              '--- Page Links ---\n- [brave-search MCP extra_snippets](https://github.com/x)'}
    evidence = classify_relevance(result, MCP_QUERY)
    assert evidence['status'] == 'unverified'
    assert evidence['sources']['body']['matched_terms'] == []
    assert evidence['links']['status'] == 'lexical_match'


@pytest.mark.parametrize('query,text,status', [
    ('numpy', 'NumPy reference guide', 'lexical_match'),

    ('intitle:"generic search docs" quasar', 'generic search docs', 'unverified'),
    ('intitle:"generic search docs" quasar', 'quasar', 'lexical_match'),
    ('quasar context', 'Quasar documentation', 'lexical_match'),
    ('R2E-Gym hybrid verifiers SWE agents', 'R2E Gym benchmark', 'lexical_match'),
    ('extra_snippets response summary results', 'extra-snippets parameter', 'lexical_match'),
    ('"Brave Search" response summary results', 'Brave Search API', 'lexical_match'),
    ('"Brave Search" response summary results', 'Search documentation', 'weak_match'),
    ('numpy OR pandas', 'pandas reference', 'lexical_match'),
    ('"numpy OR pandas"', 'pandas', 'weak_match'),
    ('site:github.com filetype:pdf intitle:docs after:2025-01-01 quasar',
     'GitHub PDF docs 2025', 'unverified'),
    ('"site:github.com"', 'site:github.com', 'lexical_match'),
    ('模型上下文 参数说明', '模型上下文长度说明', 'lexical_match'),
    ('quasar context', '模型参数：上下文一百万', 'unverified'),
    ('模型上下文', 'Model parameter reference', 'unverified'),
])
def test_subjects_short_queries_aliases_multilingual_and_operators(query, text, status):
    assert classify_relevance({'title': text}, query)['status'] == status


def test_structured_syntax_and_exclusions_not_ordinary_query_words():
    assert query_terms('site:github.com filetype:pdf inurl:docs after:2025 OR quasar -garden') == {'quasar'}

    assert query_terms('intitle:"generic search docs" -"garden tomatoes" quasar') == {'quasar'}
    assert {'site', 'github'} <= query_terms('"site:github.com"')
    # Prefetch remains the separate conservative zero-overlap gate.
    good = {'title': 'quasar'}
    junk = {'title': 'GitHub PDF docs 2025'}
    assert partition_fetchable('site:github.com filetype:pdf quasar latency',
                              [good, junk], min_fetch=0) == ([good], [junk])


def test_annotation_can_be_repeated_after_fetch_without_losing_other_fields():
    result = {'title': 'Reference', 'custom': 7}
    annotate_relevance(result, MCP_QUERY)
    assert result['relevance'] == 'unverified'
    result['full_content'] = 'Brave Search MCP server reference'
    annotate_relevance(result, MCP_QUERY)
    assert result['relevance'] == 'lexical_match'
    assert result['custom'] == 7


def test_cross_language_fetched_presence_is_not_semantic_rejection_or_upgrade():
    result = {'title': '模型参数', 'full_content': '上下文一百万' * 100}
    evidence = classify_relevance(result, 'quasar context')
    assert evidence['status'] == 'unverified'
    assert evidence['body_present'] is True
    assert evidence['coverage'] == 0
    assert 'rejected' not in evidence

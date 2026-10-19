"""Source identity survives budgets; provenance never invents confidence."""

from copy import deepcopy

import pytest

from tofu_search.passages import select_relevant_passages
from tofu_search.search.format import format_search_for_tool_response as fmt

pytestmark = pytest.mark.unit


def result(i=0, content='', snippet=''):
    return {'title': f'Quasar source {i}', 'url': f'https://source{i}.test/article',
            'source': 'Bing', 'full_content': content, 'snippet': snippet}


def bounded_fixtures():
    """Deterministic fixtures shared by tests and the output-size measurement."""
    body = ('menu account subscribe\n\n' * 500
            + 'Quasar latency is exactly 17 milliseconds in the benchmark.\n\n'
            + 'footer unrelated\n\n' * 300
            + '--- Page Links ---\n' + 'https://navigation.test/quasar\n' * 500)
    return {
        'fetched': [result(i, content=body) for i in range(3)],
        'snippet_only': [result(i, snippet='Quasar latency benchmark details ' * 500)
                         for i in range(6)],
        'mixed': [result(0, content=body), result(1, snippet='Quasar latency ' * 1000)],
    }


def payloads(rendered):
    """Extract body/snippet payloads, excluding metadata and omission notices."""
    out = []
    for entry in rendered.split('\n\n────\n\n'):
        if '\n    Summary: ' in entry:
            value = entry.split('\n    Summary: ', 1)[1].split('\n    ', 1)[0]
        elif ' chars) ────\n' in entry:
            value = entry.split(' chars) ────\n', 1)[1].split('\n    ', 1)[0]
        else:
            continue
        out.append(value)
    return out


@pytest.mark.parametrize('budget', [0, 1, 7, 120, 3000])
@pytest.mark.parametrize('kind', ['fetched', 'snippet_only', 'mixed'])
def test_all_payloads_share_budget_without_truncating_identity(kind, budget):
    rows = bounded_fixtures()[kind]
    original = deepcopy(rows)
    rendered = fmt(rows, query='quasar latency benchmark', max_total_content_chars=budget)
    assert sum(map(len, payloads(rendered))) <= budget
    for row in rows:
        assert row['title'] in rendered
        assert f"URL: {row['url']}\n" in rendered
    assert rows == original
    assert '--- Page Links ---' not in rendered
    assert 'https://navigation.test' not in rendered


def test_per_result_cap_bounds_snippets_as_well_as_bodies():
    rows = bounded_fixtures()['mixed']
    shown = payloads(fmt(rows, query='quasar latency', max_chars_per_result=90))
    assert len(shown) == 2 and all(len(text) <= 90 for text in shown)


def test_complete_default_preserves_body_and_navigation_inventory():
    rows = bounded_fixtures()['mixed']
    rendered = fmt(rows, query='quasar latency')
    assert rows[0]['full_content'] in rendered
    assert rows[1]['snippet'] in rendered
    assert 'Full Page Content' in rendered


def test_optional_provenance_is_only_rendered_when_supplied():
    row = result(snippet='Quasar details')
    plain = fmt([row])
    for label in ('Published:', 'Modified:', 'Retrieved at:', 'Fetch status:', 'Source rank:'):
        assert label not in plain
    row.update(published_date='2026-09-30', modified_date='2026-10-01',
               retrieved_at='2026-10-11T05:00:00Z', fetch_status='not_requested', source_rank=2)
    rendered = fmt([row], max_total_content_chars=0)
    for value in ('2026-09-30', '2026-10-01', '2026-10-11T05:00:00Z', 'not_requested'):
        assert value in rendered
    assert 'Source rank: 2' in rendered and 'not relevance confidence' in rendered
    assert 'failed' not in rendered.lower()


@pytest.mark.parametrize('status', ['weak_match', 'unverified'])
def test_weak_relevance_is_explicit_uncertainty_not_verification(status):
    row = result(content='Potentially useful cross-language page')
    row.update(relevance=status, relevance_evidence={
        'matched_terms': ['quasar'], 'query_terms': ['quasar', 'latency', 'benchmark']})
    rendered = fmt([row])
    assert status in rendered
    assert 'Query relevance is uncertain' in rendered
    assert 'not a factual verification' in rendered
    assert 'Lexical overlap: 1/3' in rendered


def test_duplicates_do_not_claim_independent_corroboration():
    row = result(content='Quasar facts')
    row.update(sources=['Bing', 'SearXNG', 'Bing'], upstream_engines=['bing', 'google', 'bing'],
               duplicate_urls=['https://mirror.test/a', 'https://mirror.test/a'])
    rendered = fmt([row], max_total_content_chars=1)
    assert 'independent' not in rendered and 'consensus' not in rendered
    assert 'Bing, SearXNG' in rendered and 'Bing, SearXNG, Bing' not in rendered
    assert 'may share underlying engines' in rendered
    assert 'bing, google' in rendered
    assert rendered.count('https://mirror.test/a') == 1
    assert 'not additional corroboration' in rendered


def test_rejected_candidates_explain_uncertainty_and_preserve_followup_identity():
    diag = {'reason': 'no_usable_evidence', 'constraint_rejected': 2, 'unverified_count': 1,
            'weak_match_count': 1, 'fallback_attempts': ['DDG-HTML'],
            'reason_detail': 'Returned candidates did not establish query-relevant evidence.',
            'unverified_candidates': [{'title': 'Garden index', 'url': 'https://garden.test/a'}]}
    rendered = fmt([], diag, max_total_content_chars=0)
    assert 'Excluded 2' in rendered

    assert 'Weak-match candidates: 1' in rendered
    assert 'query relevance uncertain' in rendered
    assert 'Recovery attempted: DDG-HTML' in rendered
    assert 'not evidence of absence' in rendered
    assert 'not evidence of relevance or absence' in rendered
    assert 'Garden index' in rendered and 'https://garden.test/a' in rendered
    assert diag['reason_detail'] in rendered
    assert 'does not establish' in rendered


def test_tiny_passage_budget_chooses_late_fact_not_navigation():
    raw = ('Navigation account settings\n\n' * 150
           + 'Quasar latency benchmark: 17 milliseconds.\n\n'
           + 'footer\n\n' * 100)
    shown = select_relevant_passages(raw, 'quasar latency benchmark', 60)
    assert '17 milliseconds' in shown
    assert len(shown) <= 60


def test_bounded_fixture_output_is_materially_smaller_than_complete_default():
    for rows in bounded_fixtures().values():
        full = fmt(rows, query='quasar latency benchmark')
        bounded = fmt(rows, query='quasar latency benchmark', max_total_content_chars=3000)
        assert len(bounded) < len(full) / 3
        assert sum(map(len, payloads(bounded))) <= 3000

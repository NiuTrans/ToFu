"""Snippet-only ranking retains upstream relevance without counting query echo as truth."""
from copy import deepcopy

import pytest

from tofu_search.search import orchestrator
from tofu_search.search.rerank import rerank_by_bm25

pytestmark = pytest.mark.unit

QUERY = 'Which city hosted the international robotics meeting?'


def candidates():
    return [
        {'url': 'https://report.example/meeting', 'title': 'Annual robotics meeting report',
         'snippet': 'The international robotics meeting took place in Meridian.'},
        {'url': 'https://collection.example/topic',
         'title': 'Which city hosted the international robotics meeting?',
         'snippet': 'Explore posts about which city hosted the international robotics meeting.'},
    ]


def test_snippet_query_echo_does_not_overrule_stronger_upstream_placement():
    rows = candidates()
    rows[0].update(rrf_score=1 / 61, engine_count=1)
    rows[1].update(rrf_score=1 / 68, engine_count=1)
    original = deepcopy(rows)
    ranked = rerank_by_bm25(QUERY, rows, 2)
    assert ranked[0] is rows[0]
    assert rows == original


def test_public_search_uses_engine_positions_for_snippet_order(monkeypatch):
    rows = candidates()
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *args, **kwargs: rows)
    ranked = orchestrator.perform_web_search(
        QUERY, max_results=6, engines=['Bing'], fetch_pages=False,
        filter_pages=False, deepen=False,
    )
    assert [row['url'] for row in ranked] == [rows[0]['url']]
    assert [row['url'] for row in ranked._unverified_candidates] == [rows[1]['url']]


def test_public_search_keeps_upstream_gap_when_metadata_repeats_more_terms(monkeypatch):
    report = {'url': 'https://laboratory.example/report', 'title': 'Laboratory observations',
              'snippet': 'Lunar isotope measurements appear in Table 7.'}
    keyword_page = {'url': 'https://platform.example/catalog',
                    'title': 'Lunar isotope atlas measurements',
                    'snippet': 'Design ordering catalog copy deposit terms app schedule '
                               'dashboard map product licensing.'}
    unrelated = [{'url': f'https://garden{index}.example/notes',
                  'title': f'Gardening variety {index}',
                  'snippet': 'Tomato cultivation notes'} for index in range(6)]
    rows = [report, *unrelated, keyword_page]
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *args, **kwargs: rows)
    ranked = orchestrator.perform_web_search(
        'lunar isotope atlas measurements', max_results=6, engines=['Bing'],
        fetch_pages=False, filter_pages=False, deepen=False,
    )
    assert [row['url'] for row in ranked] == [report['url'], keyword_page['url']]


def test_query_without_subject_keeps_the_existing_order():
    rows = candidates()
    rows[0]['rrf_score'] = 1 / 70
    rows[1]['rrf_score'] = 1 / 61
    assert rerank_by_bm25('site:example.org', rows, 2) == rows


def test_source_counts_do_not_double_count_the_aggregated_rank_vote():
    rows = [
        {'url': 'https://earlier.example/page', 'title': 'Lunar isotope atlas',
         'snippet': 'Measurement reference', 'rrf_score': 1 / 61, 'engine_count': 1},
        {'url': 'https://later.example/page', 'title': 'Lunar isotope atlas',
         'snippet': 'Measurement reference', 'rrf_score': 1 / 68, 'engine_count': 3},
    ]
    assert rerank_by_bm25('lunar isotope atlas', rows, 2)[0] is rows[0]


def test_upstream_votes_cannot_promote_a_zero_evidence_candidate():
    rows = [
        {'url': 'https://popular.example/page', 'title': 'Garden tools',
         'snippet': 'Growing tomatoes', 'rrf_score': 6 / 61, 'engine_count': 6},
        {'url': 'https://reference.example/page', 'title': 'Lunar isotope atlas',
         'snippet': 'Measurement reference', 'rrf_score': 1 / 70, 'engine_count': 1},
    ]
    assert rerank_by_bm25('lunar isotope atlas', rows, 2)[0] is rows[1]


@pytest.mark.parametrize('upstream', [False, True])
def test_fetched_body_evidence_retains_lexical_ranking(upstream):
    rows = [
        {'url': 'https://notes.example/page', 'title': 'Lunar isotope notes',
         'snippet': 'Sample collection methods', 'rrf_score': 3 / 61 if upstream else 0,
         'engine_count': 3 if upstream else 1},
        {'url': 'https://atlas.example/page', 'title': 'Measurement reference',
         'snippet': 'Map release', 'full_content': 'The lunar isotope atlas maps sample abundance.',
         'rrf_score': 1 / 70 if upstream else 0, 'engine_count': 1},
    ]
    assert rerank_by_bm25('lunar isotope atlas', rows, 2)[0] is rows[1]

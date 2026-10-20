"""Query-only collection descriptions stay recoverable without claiming relevance."""
from copy import deepcopy

import pytest

from tofu_search.search import orchestrator
from tofu_search.search.quality import classify_relevance

pytestmark = pytest.mark.unit
QUERY = 'Who founded the Orion Research Institute?'


def collection(snippet=None, **extra):
    return {'url': 'https://collection.example/topic', 'title': QUERY,
            'snippet': snippet or f'Watch reels about {QUERY} from people around the world.',
            **extra}


@pytest.mark.parametrize('template', [
    'Watch reels about {} from people around the world.',
    'Explore posts about {}',
    'Browse videos about {}',
])
def test_query_only_listing_is_uncertain_even_with_complete_overlap(template):
    row = collection(template.format(QUERY))
    original = deepcopy(row)
    evidence = classify_relevance(row, QUERY)
    assert evidence['status'] == 'unverified'
    assert evidence['sources']['serp']['query_echo_only'] is True
    assert evidence['coverage'] == 1.0
    assert row == original


@pytest.mark.parametrize('addition', [
    'It was founded by Mira Vale.',
    'The institute was founded in 1987.',
    'It was not founded by Mira Vale.',
    'The founder is unknown.',
    'It had one founder.',
    'No.',
    'One.',
])
def test_facts_and_qualifications_beyond_query_echo_remain_evidence(addition):
    row = collection(f'Explore posts about {QUERY} {addition}')
    evidence = classify_relevance(row, QUERY)
    assert evidence['status'] == 'lexical_match'
    assert not evidence['sources']['serp'].get('query_echo_only')


def test_faq_question_title_and_short_answer_are_not_a_query_only_listing():
    evidence = classify_relevance(collection('Mira Vale.'), QUERY)
    assert evidence['status'] == 'lexical_match'
    assert not evidence['sources']['serp'].get('query_echo_only')


def test_new_title_evidence_is_preserved():
    row = collection()
    row['title'] += ' — Founded by Mira Vale'
    assert classify_relevance(row, QUERY)['status'] == 'lexical_match'


def test_fetched_body_can_establish_relevance_after_query_echo():
    row = collection(full_content='Mira Vale founded the Orion Research Institute.')
    evidence = classify_relevance(row, QUERY)
    assert evidence['sources']['serp']['status'] == 'unverified'
    assert evidence['sources']['body']['status'] == 'lexical_match'
    assert evidence['status'] == 'lexical_match'


def test_public_search_preserves_listing_as_uncertain_followup(monkeypatch):
    listing = collection()
    answer = {'url': 'https://history.example/institute', 'title': 'Orion Institute history',
              'snippet': 'Mira Vale founded the Orion Research Institute.'}
    monkeypatch.setattr(orchestrator, 'search_bing', lambda *args, **kwargs: [listing, answer])
    results = orchestrator.perform_web_search(
        QUERY, engines=['Bing'], fetch_pages=False, filter_pages=False, deepen=False,
    )
    assert [row['url'] for row in results] == [answer['url']]
    uncertain = results._search_diag['unverified_candidates']
    assert [row['url'] for row in uncertain] == [listing['url']]
    assert results._unverified_candidates[0]['snippet'] == listing['snippet']

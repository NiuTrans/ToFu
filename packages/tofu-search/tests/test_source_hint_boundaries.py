"""URL hints never certify ownership or replace evidence about the query."""

import pytest

from tofu_search.search.authority import authority_label, classify_authority
from tofu_search.search.format import format_search_for_tool_response
from tofu_search.search.rerank import rerank_by_bm25

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('host', [
    'notpython.org', 'notgithub.com', 'evilpostgresql.org',
    'notietf.org', 'notreadthedocs.io',
])
def test_reference_host_requires_a_dns_label_boundary(host):
    assert classify_authority(f'https://{host}/article') == 'neutral'


@pytest.mark.parametrize('url', [
    'https://python.org/library',
    'https://docs.python.org/library',
    'https://PYTHON.ORG.:443/library',
    'https://user:password@docs.python.org/library',
])
def test_reference_host_normalizes_the_actual_hostname(url):
    assert classify_authority(url) == 'reference_host'


@pytest.mark.parametrize('url', [
    'https://python.org@unrelated.example/article',
    'https://python.org.unrelated.example/article',
    'https://[::1]/', 'https://127.0.0.1/',
    'https://python.org:invalid/', 'file://python.org/article',
])
def test_misleading_or_invalid_urls_have_no_reference_hint(url):
    assert classify_authority(url, 'task cancellation') == 'neutral'


@pytest.mark.parametrize(('url', 'query'), [
    ('https://python.unrelated.example/article', 'Python cancellation'),
    ('https://docs.unrelated.example/article', 'task cancellation'),
    ('https://github.com/unrelated/article', 'task cancellation'),
    ('https://latencycost.com/article', 'task cancellation'),
])
def test_source_hints_do_not_certify_page_authority(url, query):
    label = authority_label(url, query)
    assert label and 'unverified' in label.lower()
    rendered = format_search_for_tool_response([{
        'url': url, 'title': 'Candidate', 'snippet': 'Candidate evidence.',
        'source': 'Test',
    }], query=query)
    assert label in rendered
    assert 'OFFICIAL SOURCE' not in rendered
    assert 'PRIMARY SOURCE' not in rendered


def _result(url, title, snippet, **extra):
    return {'url': url, 'title': title, 'snippet': snippet, 'source': 'Test', **extra}


@pytest.mark.parametrize('extra', [
    {},
    {'engine_count': 64, 'rrf_score': 1.0},
    {'full_content': 'Sunny weather for the weekend.'},
])
def test_priors_cannot_promote_a_result_without_query_evidence(extra):
    irrelevant = _result(
        'https://python.example.net/', 'Unrelated weather',
        'Sunny weather for the weekend.', **extra)
    answer = _result(
        'https://reference.example/task-cancellation', 'Python cancellation',
        'Python coroutine cancellation raises CancelledError inside the task.')
    ranked = rerank_by_bm25('Python cancellation', [irrelevant, answer], 1)
    assert ranked == [answer]


def test_reference_prior_does_not_replace_the_missing_query_aspect():
    overview = _result(
        'https://docs.python.org/', 'Python', 'A general overview.')
    answer = _result(
        'https://reference.example/task-cancellation', 'Python cancellation',
        'Cancellation raises CancelledError inside the Python task.')
    assert rerank_by_bm25('Python cancellation', [overview, answer], 1) == [answer]


def test_entity_coverage_cannot_reserve_a_slot_for_no_query_evidence():
    python = _result(
        'https://python.org/tasks', 'Python cancellation',
        'Python cancellation raises CancelledError.')
    irrelevant = _result(
        'https://rust.example/', 'Garden', 'Growing vegetables.',
        engine_count=64, rrf_score=1.0)
    comparison = _result(
        'https://reference.example/cancel', 'Python Rust cancellation',
        'Python raises CancelledError; Rust cancellation drops a future.')
    selected = rerank_by_bm25(
        'Python Rust cancellation', [python, irrelevant, comparison], 2)
    assert irrelevant not in selected
    assert python in selected and comparison in selected


def test_domain_diversity_does_not_replace_query_evidence_with_noise():
    answers = [
        _result(f'https://docs.python.org/tasks/{index}', 'Python cancellation',
                'Python cancellation raises CancelledError inside the task.')
        for index in range(3)
    ]
    unrelated = _result(
        'https://different.example/', 'Weather', 'Sunny weather this weekend.')
    selected = rerank_by_bm25('Python cancellation', [*answers, unrelated], 3)
    assert unrelated not in selected
    assert selected == answers


def test_domain_name_penalty_cannot_erase_matching_evidence():
    irrelevant = _result(
        'https://docs.python.org/', 'Gardening', 'Growing vegetables.')
    answer = _result(
        'https://costbench.example/', 'Quasar', 'Quasar costs 17 units.')
    assert rerank_by_bm25('quasar', [irrelevant, answer], 1) == [answer]

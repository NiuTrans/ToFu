"""Search diagnostics retain follow-up authority without inventing evidence."""

import json
from types import SimpleNamespace
import sys

import pytest

from evaluations.search_quality import worker
from evaluations.search_quality.contracts import RESULTS, SNIPPET_CHARS, score, tasks
from evaluations.search_quality.runner import execute_tool
from tofu_search.search.orchestrator import SearchResultList

pytestmark = pytest.mark.unit


def search(monkeypatch, rows, arm='tofu'):
    if arm == 'tofu':
        import tofu_search
        monkeypatch.setattr(tofu_search, 'perform_web_search', lambda *a, **kw: rows)
        monkeypatch.setattr(tofu_search, 'get_playwright_pool',
                            lambda: SimpleNamespace(shutdown=lambda: None))
    else:
        class DDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, *args, **kwargs):
                return rows

        monkeypatch.setitem(sys.modules, 'ddgs', SimpleNamespace(DDGS=DDGS))
        monkeypatch.setattr(worker, 'identity', lambda arm: {})
    return worker.operation({'arm': arm, 'operation': 'search', 'query': 'technical manual',
                             'proxy': '', 'seed': 1, 'region': 'us-en'})


def test_empty_primary_list_retains_uncertain_candidate_and_bounded_diagnostics(monkeypatch):
    rows = SearchResultList()
    rows._unverified_candidates = [{'url': 'https://manual.example/guide.html',
                                   'title': 'Manual', 'snippet': 'An uncertain match.'}]
    rows._search_diag = {'reason': 'no_usable_evidence', 'unverified_count': 1,
                         'engine_errors': {'engine': 'secret provider message'},
                         'reason_detail': 'secret provider message', 'deadline_hit': False}
    output = search(monkeypatch, rows)
    assert output['results'] == [{
        'url': 'https://manual.example/guide.html', 'title': 'Manual',
        'snippet': 'An uncertain match.', 'evidence_status': 'unverified_candidate'}]
    assert output['search_diagnostic']['reason'] == 'no_usable_evidence'
    assert output['search_diagnostic']['unverified_count'] == 1
    assert output['search_diagnostic']['engine_error_count'] == 1
    assert 'secret provider message' not in json.dumps(output)

    class FetchWorker:
        def call(self, *args, **kwargs):
            return {'url': 'https://manual.example/guide.html', 'text': 'Manual text.'}

    calls = [{'name': 'custom__search', 'accepted': True, 'is_error': False, 'output': output}]
    fetched = execute_tool(FetchWorker(), 'tofu', tasks()[0], 'custom__fetch',
                           {'url': output['results'][0]['url']}, calls, 10)
    assert fetched['text'] == 'Manual text.'
    # Discovery is fetch authority, never a fetched-source quotation by itself.
    assert not score(tasks()[0], '{}', calls)['passed']


def test_primary_candidates_precede_uncertain_followups_with_one_shared_budget(monkeypatch):
    rows = SearchResultList([{'url': 'https://manual.example/main', 'title': 'Main'}])
    rows._unverified_candidates = [rows[0]] + [
        {'url': f'https://manual.example/{i}', 'title': 'x' * 600,
         'snippet': 's' * (SNIPPET_CHARS + 1)} for i in range(RESULTS + 2)]
    output = search(monkeypatch, rows)
    assert len(output['results']) == RESULTS
    assert output['results'][0]['url'] == rows[0]['url']
    assert output['results'][0]['evidence_status'] == 'search_snippet'
    assert len({r['url'] for r in output['results']}) == RESULTS
    assert all(len(r['title']) <= 500 and len(r['snippet']) <= SNIPPET_CHARS
               for r in output['results'])
    assert all(r['evidence_status'] == 'unverified_candidate' for r in output['results'][1:])


@pytest.mark.parametrize('arm', ['tofu', 'ddgs'])
def test_candidate_projection_never_invents_a_truncated_url(monkeypatch, arm):
    url_field, snippet_field = ('href', 'body') if arm == 'ddgs' else ('url', 'snippet')
    urls = ['', 'https://manual.example/' + 'a' * 4000,
            'https://name:password@manual.example/', 'https://manual.example/real']
    rows = [{url_field: url, 'title': 'Manual', snippet_field: 'Details.'} for url in urls]
    output = search(monkeypatch, rows, arm)
    assert [r['url'] for r in output['results']] == [urls[-1]]
    assert output['results'][0]['evidence_status'] == 'search_snippet'


def test_observed_quote_over_length_limit_has_an_independent_failure_reason():
    task = tasks()[1]
    url = task['reference_url']
    quote = 'JSONB begins in 3.45.0. ' + 'Extra context. ' * 30
    calls = [
        {'name': 'custom__search', 'accepted': True, 'output': {'results': [{'url': url}]}},
        {'name': 'custom__fetch', 'accepted': True, 'output': {'url': url, 'text': quote}},
    ]
    answer = {key: {'value': '3.45.0', 'url': url, 'quote': quote} for key in task['facts']}
    result = score(task, json.dumps(answer), calls)
    assert result['facts']['introduced']['quote_observed'] is True
    assert result['facts']['introduced']['quote_length_valid'] is False
    assert not result['passed']

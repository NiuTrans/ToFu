"""Backend arXiv failures stay distinct from clean empty results.

The browser half is covered by the native Vite arXiv-search contract; this
module keeps the library, route and API-client boundaries focused.
"""

from __future__ import annotations

import os
import re

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _patch_ts_search(monkeypatch, fake):
    from tofu_search.search.vertical import arxiv as ts_arxiv

    monkeypatch.setattr(ts_arxiv, 'search_by_query', fake)


def _envelope(outcome, papers=(), error=''):
    return {
        'ok': outcome in ('hits', 'no_matches'),
        'query': 'all:q',
        'mode': 'terms',
        'papers': list(papers),
        'outcome': outcome,
        'error': error,
    }


def _paper(index):
    return {
        'arxiv_id': f'2301.0000{index}',
        'title': f'Real Paper {index}',
        'authors': ['A. Author'],
        'summary': 's',
        'published': '2023-01-01',
        'primary_category': 'cs.CL',
        'pdf_url': '',
        'abs_url': '',
    }


def test_lib_hits_return_results_and_empty_error(monkeypatch):
    import lib.paper.arxiv as arxiv

    _patch_ts_search(
        monkeypatch,
        lambda *args, **kwargs: _envelope('hits', [_paper(1), _paper(2)]),
    )
    results, error = arxiv.search_arxiv_explained('real paper', max_results=5)
    assert error == ''
    assert [row['arxiv_id'] for row in results] == [
        '2301.00001',
        '2301.00002',
    ]


def test_lib_no_matches_is_clean_empty(monkeypatch):
    import lib.paper.arxiv as arxiv

    _patch_ts_search(
        monkeypatch,
        lambda *args, **kwargs: _envelope('no_matches'),
    )
    assert arxiv.search_arxiv_explained('missing', max_results=5) == ([], '')


@pytest.mark.parametrize(
    ('outcome', 'reason'),
    [
        ('request_failed', 'HTTP 429'),
        ('unusable_query', ''),
    ],
)
def test_lib_failures_are_not_silent_empty(monkeypatch, outcome, reason):
    import lib.paper.arxiv as arxiv

    monkeypatch.setattr('time.sleep', lambda seconds: None)
    _patch_ts_search(
        monkeypatch,
        lambda *args, **kwargs: _envelope(outcome, error=reason),
    )
    results, error = arxiv.search_arxiv_explained('q', max_results=5)
    assert results == []
    assert error


def test_lib_wrapper_keeps_list_only_contract(monkeypatch):
    import lib.paper.arxiv as arxiv

    monkeypatch.setattr('time.sleep', lambda seconds: None)
    _patch_ts_search(
        monkeypatch,
        lambda *args, **kwargs: _envelope(
            'request_failed',
            error='HTTP 500',
        ),
    )
    assert arxiv.search_arxiv('q', max_results=5) == []
    with pytest.raises(arxiv.ArxivQuerySyntaxError):
        arxiv.search_arxiv('ti:attention AND all:"kv cache"')


def _patch_route(monkeypatch, implementation):
    import routes.paper as paper_routes

    monkeypatch.setattr(
        paper_routes,
        'search_arxiv_explained',
        implementation,
    )


def test_route_success_and_clean_empty(flask_client, monkeypatch):
    _patch_route(monkeypatch, lambda query, count: ([_paper(1)], ''))
    response = flask_client.post(
        '/api/v1/paper/search-arxiv',
        json={'query': 'real paper'},
    )
    assert response.status_code == 200
    assert response.get_json()['results'][0]['arxiv_id'] == '2301.00001'

    _patch_route(monkeypatch, lambda query, count: ([], ''))
    response = flask_client.post(
        '/api/v1/paper/search-arxiv',
        json={'query': 'missing'},
    )
    assert response.status_code == 200
    assert response.get_json() == {
        'ok': True,
        'query': 'missing',
        'results': [],
    }


def test_route_upstream_failure_preserves_reason(flask_client, monkeypatch):
    _patch_route(monkeypatch, lambda query, count: ([], 'HTTP 429'))
    response = flask_client.post(
        '/api/v1/paper/search-arxiv',
        json={'query': 'q'},
    )
    assert response.status_code == 502
    body = response.get_json()
    assert body['ok'] is False
    assert 'HTTP 429' in body['error']


def test_route_syntax_failure_is_400(flask_client, monkeypatch):
    import routes.paper as paper_routes

    def reject(query, count):
        raise paper_routes.ArxivQuerySyntaxError('built syntax rejected')

    _patch_route(monkeypatch, reject)
    response = flask_client.post(
        '/api/v1/paper/search-arxiv',
        json={'query': 'ti:x AND all:"y"'},
    )
    assert response.status_code == 400
    assert response.get_json()['ok'] is False


def test_route_unexpected_failure_is_structured_502(flask_client, monkeypatch):
    def fail(query, count):
        raise AttributeError("no attribute 'search_by_query'")

    _patch_route(monkeypatch, fail)
    response = flask_client.post(
        '/api/v1/paper/search-arxiv',
        json={'query': 'q'},
    )
    assert response.status_code == 502
    body = response.get_json()
    assert body['ok'] is False
    assert 'search_by_query' in body['error']


def test_api_search_does_not_swallow_errors():
    source = open(runtime_section_path('api.js'), encoding='utf-8').read()
    match = re.search(r"searchArxiv:[\s\S]{0,300}?post\(([^)]*)\)", source)
    assert match, 'Api.paper.searchArxiv entry not found'
    assert "'null'" not in match.group(0)
    assert 'onError' not in match.group(0)

"""Offline citation graph, canonical identity, source and budget contracts."""
import pytest

from lib.research.discovery import discover_corpus, MAX_CITATION_CALLS, max_citation_calls
from lib.research.literature import AcademicDiscovery, MAX_API_REQUESTS

pytestmark = pytest.mark.unit


def test_real_forward_backward_edges_cycles_and_relevance():
    calls = []
    def graph(paper, *, direction, max_results):
        calls.append((paper['doi'], direction))
        if paper['doi'] == '10.1234/core':
            return [{'doi': '10.1234/ref' if direction == 'references' else '10.1234/new'}]
        return [{'doi': '10.1234/core'}]
    result = discover_corpus('tool recovery', target=20, search=lambda *a, **k: [
        {'doi': '10.1234/other', 'title': 'Unrelated'},
        {'doi': 'https://doi.org/10.1234/CORE', 'title': 'tool recovery'}], citation_search=graph)
    assert calls[0] == ('10.1234/core', 'references')
    assert len(calls) == len(set(calls)) <= MAX_CITATION_CALLS
    assert {'from': 'doi:10.1234/core', 'to': 'doi:10.1234/ref', 'relation': 'cites'} in result['citation_edges']
    assert {'from': 'doi:10.1234/new', 'to': 'doi:10.1234/core', 'relation': 'cites'} in result['citation_edges']
    assert result['discovered_count'] == 4
    assert not any(p['ingest_supported'] for p in result['papers'])


def test_bridge_dedup_preserves_sources_and_arxiv_ingest_identity():
    result = discover_corpus('topic', target=10, search=lambda *a, **k: [
        {'arxiv_id': '2501.00001v2', 'source': 'arxiv'},
        {'doi': '10.1234/ABC', 'source': 'crossref'},
        {'doi': 'https://doi.org/10.1234/abc', 'arxiv_id': '2501.00001v3', 'source': 'semantic_scholar'}])
    assert result['discovered_count'] == 1
    paper = result['papers'][0]
    assert paper['arxiv_id'] == '2501.00001'
    assert paper['doi'] == '10.1234/abc'
    assert paper['ingest_supported']
    assert len(paper['retrieval_provenance']) == 3


def test_adapter_fake_payloads_source_failure_and_graph_default():
    urls = []
    def get_json(url, params):
        urls.append(url)
        if 'crossref' in url:
            raise TimeoutError('offline source failure')
        if '/references' in url:
            return {'data': [{'citedPaper': {'paperId': 'b', 'title': 'Reference', 'externalIds': {'DOI': '10.1234/ref'}}}]}
        if '/citations' in url:
            return {'data': []}
        return {'data': [{'paperId': 'a', 'title': 'Recovery', 'externalIds': {'DOI': '10.1234/core'}}]}
    adapter = AcademicDiscovery(lambda *a, **k: [{'arxiv_id': '2501.00001'}], get_json=get_json)
    result = discover_corpus('Recovery', target=12, search=adapter.search)
    assert result['graph_status'] == 'enabled'
    assert any('/references' in url for url in urls)
    assert any('/citations' in url for url in urls)
    assert any(s['source'] == 'crossref' and s['status'] == 'unavailable' for s in result['source_statuses'])
    assert any(p['doi'] == '10.1234/ref' for p in result['papers'])


def test_crossref_metadata_retained_on_semantic_failure():
    def get_json(url, params):
        if 'crossref' in url:
            return {'message': {'items': [{'DOI': '10.1234/published', 'title': ['Published work']}]}}
        raise RuntimeError('unavailable')
    adapter = AcademicDiscovery(lambda *a, **k: [], get_json=get_json)
    result = discover_corpus('Published', target=6, search=adapter.search)
    assert result['papers'][0]['title'] == 'Published work'
    assert result['papers'][0]['reading_status'] == 'metadata_only'
    assert not result['papers'][0]['ingest_supported']


def test_graph_cancellation_prevents_next_direction():
    cancelled = False
    calls = []
    def graph(*a, **k):
        nonlocal cancelled
        calls.append(k['direction'])
        cancelled = True
        return [{'doi': '10.1234/ref'}]
    result = discover_corpus('topic', target=10,
        search=lambda *a, **k: [{'doi': '10.1234/core'}],
        citation_search=graph, abort_check=lambda: cancelled)
    assert calls == ['references']
    assert result['stop_reason'] == 'aborted'
    assert result['discovered_count'] == 1


def test_infinite_graph_iterator_target_and_hard_cap():
    consumed = []
    def graph(*a, **k):
        for i in range(10000):
            consumed.append(i)
            yield {'doi': f'10.1234/{i}'}
    result = discover_corpus('topic', target=3,
        search=lambda *a, **k: [{'doi': '10.1234/core'}], citation_search=graph)
    assert len(consumed) == 2
    assert result['stop_reason'] == 'corpus_budget'
    assert discover_corpus('topic', target=10000, search=lambda *a, **k: [])['target_count'] == 200


def test_graph_errors_are_finite_and_visible():
    def graph(*a, **k):
        raise RuntimeError('graph unavailable')
    result = discover_corpus('topic', target=200, search=lambda *a, **k: [
        {'doi': f'10.1234/{i}'} for i in range(10)], citation_search=graph)
    # The graph budget scales with the corpus target; failing adapters can
    # exhaust the (bounded) core set before the budget is spent.
    assert 0 < result['graph_calls'] <= max_citation_calls(200)
    assert result['stop_reason'] in ('graph_budget', 'graph_exhausted')
    assert all(r['error'] == 'RuntimeError' for r in result['rounds'] if 'parent_id' in r)


def test_api_budget_no_retries_and_abort():
    calls = []
    adapter = AcademicDiscovery(lambda *a, **k: [], get_json=lambda *a: calls.append(a) or {'data': []})
    for _ in range(MAX_API_REQUESTS + 3):
        adapter.citation_search({'doi': '10.1234/a'}, direction='references', max_results=40)
    assert len(calls) == MAX_API_REQUESTS
    assert adapter.statuses[-1]['status'] == 'unavailable'
    adapter.abort_check = lambda: True
    with pytest.raises(InterruptedError):
        adapter.search('topic', max_results=20)


def test_depth_limit_does_not_expand_third_hop():
    calls = []
    def graph(paper, *, direction, max_results):
        calls.append(paper['depth'])
        return [{'doi': f"10.1234/depth{paper['depth'] + 1}"}]
    result = discover_corpus('topic', target=20,
        search=lambda *a, **k: [{'doi': '10.1234/depth0'}], citation_search=graph)
    assert max(calls) == 1
    assert result['stop_reason'] == 'graph_exhausted'

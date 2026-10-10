"""Discovery capacity is independent of model context; search is never proof."""
import pytest

pytestmark = pytest.mark.unit


def test_diverse_search_can_discover_two_hundred_with_bounded_adapters():
    from lib.research.discovery import discover_corpus
    calls = []
    def search(query, max_results):
        batch = len(calls)
        calls.append((query, max_results))
        return ({'arxiv_id': f'2501.{batch * 40 + i:05d}'} for i in range(10000))
    result = discover_corpus('small model tool recovery', target=200, search=search)
    assert len(calls) == 5
    assert all(limit <= 40 for _, limit in calls)
    assert result['discovered_count'] == 200
    assert result['stop_reason'] == 'corpus_budget'
    assert result['coverage_complete'] is False
    assert all(paper['reading_status'] == 'metadata_only' for paper in result['papers'])
    assert len({query for query, _ in calls}) == 5


def test_duplicate_saturation_preserves_provenance_not_novelty_claim():
    from lib.research.discovery import discover_corpus
    result = discover_corpus('recovery', target=200, search=lambda *a, **k: [
        {'arxiv_id': '2501.00001v2', 'title': 'One'},
        {'arxiv_id': '2501.00001v3', 'title': 'Same'},
    ])
    assert result['discovered_count'] == 1
    assert len(result['rounds']) == 3
    assert result['stop_reason'] == 'diminishing_discovery'
    assert result['coverage_complete'] is False


def test_failed_searches_are_coverage_holes_and_bounded():
    from lib.research.discovery import discover_corpus
    def search(*args, **kwargs):
        raise RuntimeError('transport unavailable')
    result = discover_corpus('recovery', target=200, search=search)
    assert len(result['rounds']) == 6
    assert all(row['error'] == 'RuntimeError' for row in result['rounds'])
    assert result['stop_reason'] == 'query_budget'


def test_abort_does_not_invoke_adapter():
    from lib.research.discovery import discover_corpus
    result = discover_corpus('recovery', target=200,
        search=lambda *a, **k: pytest.fail('search after abort'), abort_check=lambda: True)
    assert result['stop_reason'] == 'aborted'
    assert result['papers'] == []


def test_small_target_and_oversized_iterator_are_bounded():
    from lib.research.discovery import discover_corpus
    consumed = []
    def search(*args, **kwargs):
        for i in range(10000):
            consumed.append(i)
            yield {'arxiv_id': f'2501.{i:05d}'}
    result = discover_corpus('recovery', target=3, search=search)
    assert len(consumed) == 3
    assert result['discovered_count'] == 3


def test_request_accepts_large_corpus_but_still_has_hard_limit():
    from lib.research.contracts import normalize_research_request
    request = normalize_research_request('research', seed_arxiv_ids=[
        f'2501.{i:05d}' for i in range(150)])
    assert len(request.seed_arxiv_ids) == 150


def test_harvest_batches_large_corpus_without_enlarging_parser_boundary(monkeypatch):
    import lib.paper.harvest as harvest
    import lib.research.recipe as recipe
    from lib.paper.contracts import PAPER_FANIN_MAX_PAPERS
    calls = []
    def batch(ids, **kwargs):
        calls.append(list(ids))
        return {'parsed': len(ids), 'results': [
            {'arxivId': aid, 'status': 'parsed'} for aid in ids]}
    monkeypatch.setattr(harvest, 'harvest_arxiv_batch', batch)
    ids = [f'2501.{i:05d}' for i in range(150)]
    out = recipe._harvest_batch(ids, folder_id='f', user_id=1)
    assert all(len(call) <= PAPER_FANIN_MAX_PAPERS for call in calls)
    assert [aid for call in calls for aid in call] == ids
    assert out['parsed'] == 150
    assert len(out['results']) == 150

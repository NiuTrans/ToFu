"""Offline literature wire regressions; no provider or model calls."""
import pytest

pytestmark = pytest.mark.unit


def test_provider_zero_unknown_and_bridge_metadata():
    from lib.research.literature import semantic_record, crossref_record
    from lib.research.discovery import discover_corpus
    from lib.research.records import literature_projection
    hits = [semantic_record({'paperId': 'abc', 'externalIds': {'ArXiv': '2401.00001'},
                             'title': 'One', 'citationCount': 0, 'venue': 'Actual venue', 'year': 2024}),
            crossref_record({'DOI': '10.1234/one', 'title': ['One']}),
            semantic_record({'paperId': 'abc', 'externalIds': {'ArXiv': '2401.00001', 'DOI': '10.1234/one'}}),
            crossref_record({'DOI': '10.1234/two', 'title': ['Two'],
                             'published': {'date-parts': [[2020]]}, 'container-title': ['Journal']})]
    result = discover_corpus('topic', target=4, search=lambda *a, **k: hits)
    rows = literature_projection(result)['papers']
    assert len(rows) == 2
    first = next(p for p in rows if p['arxivId'])
    assert (first['citationCount'], first['citationCountSource'], first['year'], first['venue']) == (0, 'semantic_scholar', 2024, 'Actual venue')
    second = next(p for p in rows if not p['arxivId'])
    assert second['paperId'] == 'doi:10.1234/two'
    assert second['status'] == 'metadata_only'
    assert second['citationCount'] is None
    assert second['year'] == 2020


def test_sparse_receipts_keep_graph_identity_and_zero():
    from lib.research.records import merge_paper, literature_projection
    projection = literature_projection({'papers': [
        {'paper_id': 'doi:10.1234/a', 'doi': '10.1234/a', 'arxiv_id': '2401.00001', 'citation_count': 0},
        {'paper_id': 'doi:10.1234/b', 'doi': '10.1234/b'}],
        'citation_edges': [{'from': 'doi:10.1234/a', 'to': 'doi:10.1234/b', 'relation': 'cites'}]})
    merge_paper(projection['papers'], {'arxivId': '2401.00001', 'paperId': 'harvest_id',
                                      'status': 'parsed', 'citationCount': None, 'textLength': 'bad'})
    assert projection['papers'][0]['paperId'] == projection['citation_edges'][0]['from']
    assert projection['papers'][0]['citationCount'] == 0
    assert projection['papers'][1]['status'] == 'metadata_only'


def test_old_checkpoint_recovery_only_uses_present_evidence():
    from lib.research.records import _recover_evidence
    record = {'status': 'error', 'papers': [{'arxivId': '2401.00001', 'status': 'parsed'}],
              'checkpoint': {'stages': {'harvest': {'artifact': {'discovery': {'papers': [
                  {'arxiv_id': '2401.00001', 'paper_id': 'doi:10.1234/a', 'year': 2024}]}}},
                  'survey': {'error': 'actual failure'}}}}
    _recover_evidence(record)
    assert record['papers'][0]['year'] == 2024
    assert record['papers'][0]['status'] == 'parsed'
    assert record['error'] == 'actual failure'
    assert record['failed_stage'] == 'survey'
    empty = {'status': 'error'}
    _recover_evidence(empty)
    assert 'error' not in empty and 'papers' not in empty


def test_arxiv_year_uses_published_metadata_not_identifier_guess():
    from lib.research.literature import bibliometrics
    assert bibliometrics({'source': 'arxiv', 'published': '2023-10-11'})['year'] == 2023
    assert bibliometrics({'arxiv_id': '2310.00001'})['year'] is None
    assert bibliometrics({'published': 'not a date'})['year'] is None
    assert bibliometrics({'year': 2024, 'published': '2023-10-11'})['year'] == 2024


def test_old_failure_projection_uses_stage_receipt_and_owned_live_error(monkeypatch):
    from lib.research import records, runtime
    record = {'task_id': 'old-research', 'status': 'error', 'research_progress': {
        'activity': 'stage_failed', 'stage': 'survey'}}
    monkeypatch.setattr(runtime._research_runtime, 'get', lambda _: {
        'user_id': 7, 'status': 'error',
        'error': {'message': 'Failed', 'detail': 'Actual batch error', 'raw': 'do not expose'}})
    projected = records._project(record, user_id=7)
    assert projected['error'] == 'Failed: Actual batch error'
    assert projected['failed_stage'] == 'survey'
    assert 'raw' not in projected['error']
    assert 'error' not in records._project(record, user_id=8)
    assert 'error' not in record  # read-only projection

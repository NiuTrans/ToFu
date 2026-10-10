"""Bounded survey retrieval: no network or paid model calls."""
import json

import pytest

from lib.paper import survey
from lib.paper.contracts import PAPER_SURVEY_CONTEXT_CHARS

pytestmark = pytest.mark.unit


def _papers(n):
    return [{'arxiv_id': f'2401.{i:05d}', 'title': f'Paper {i}',
             'paper_hash': f'h{i}', 'source': 'parsed_text',
             'source_version': f'2401.{i:05d}v2',
             'content': 'Generic background. ' * 180 +
                        'However recall fails under compression. ' * 40}
            for i in range(n)]


def test_large_corpus_selection_is_bounded_and_not_first_forty():
    papers = _papers(150)
    papers[-1]['title'] = 'Unique zebrafish evaluation'
    selected = survey._survey_working_set(papers, 'unique zebrafish')
    assert selected[0]['arxiv_id'] == papers[-1]['arxiv_id']
    assert len(selected) <= 20
    assert len(json.dumps([p['evidence_cards'] for p in selected])) < PAPER_SURVEY_CONTEXT_CHARS
    assert len(papers) - len(selected) >= 130


def test_support_and_counterevidence_spans_keep_original_offsets():
    papers = _papers(1)
    papers[0]['content'] = 'Recall is supported. ' + 'x' * 900 + ' However recall fails. '
    selected = survey._survey_working_set(papers, 'supported')
    cards = selected[0]['evidence_cards']
    assert any('supported' in c['quote'] for c in cards)
    assert any('fails' in c['quote'] for c in cards)
    for card in cards:
        assert papers[0]['content'][card['start']:card['end']] == card['quote']
        assert card['source_version'].endswith('v2')
        assert card['entailment'] == 'unverified'


def test_gap_synonyms_and_counterevidence_survive_identity_gate():
    row = {'claim': 'Scoped recall gap', 'rationale': 'Limited task coverage',
           'evidence': ['2401.00001'],
           'counter_evidence': [{'paper': '9999.99999', 'claim': 'May solve this'}],
           'falsification_test': 'Reproduce closest baseline',
           'uncertainties': ['Unknown source support']}
    out = survey._verify_against_library({'open_gaps': [row]}, user_id=1,
                                        lib_ids={'2401.00001'}, ground_fn=lambda _: False)
    gap = out['open_gaps'][0]
    assert gap['gap'] == row['claim']
    assert gap['why_open'] == row['rationale']
    assert gap['counterevidence'] == row['counter_evidence']
    assert gap['falsifier'] == row['falsification_test']
    assert gap['low_confidence'] is True


def test_retrieval_cancellation():
    from lib.llm_errors import AbortedError
    with pytest.raises(AbortedError):
        survey._survey_working_set(_papers(100), 'recall', lambda: True)


def test_working_set_is_order_invariant_and_supports_chinese():
    papers = _papers(100)
    papers[-1]['title'] = '工具调用错误恢复机制'
    selected = survey._survey_working_set(papers, '小模型工具调用错误恢复')
    reverse = survey._survey_working_set(list(reversed(papers)), '小模型工具调用错误恢复')
    assert selected[0]['arxiv_id'] == papers[-1]['arxiv_id']
    assert [p['arxiv_id'] for p in selected] == [p['arxiv_id'] for p in reverse]

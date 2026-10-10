"""Scientific repair is metadata-only and never a bypass of evidence judging."""
import copy
import json

import pytest

from lib.paper import ideate as it

pytestmark = pytest.mark.unit

GAPS = {'open_gaps': [{'id': 'gap_1', 'gap': 'Recall fails',
                       'evidence': ['2305.11111'], 'low_confidence': True,
                       'evidence_tiers': {'2305.11111': 'grounded'},
                       'counterevidence': ['Recall succeeds at low compression']}],
        'excluded_corpus_ids': ['2401.99999']}


def candidate():
    return dict(title='Recall compression', kind='Algorithm', linked_gap_id=' GAP_1 ',
                corpus_delta='Change arXiv:2305.11111 by conditioning rank on recall',
                core_mechanism='Recall changes rank allocation', novelty_claim='Adaptive rank',
                falsifiable_prediction='Recall exceeds 90% at 4x compression',
                why_not_AB='Allocation changes the optimization state')


def test_unique_verified_metadata_repair_and_stable_identity():
    raw = candidate()
    original = copy.deepcopy(raw)
    repaired = it._repair_candidate(raw, GAPS)
    assert repaired['linked_gap_id'] == 'gap_1'
    assert repaired['corpus_anchor_id'] == '2305.11111'
    assert len(repaired['repair_records']) == 3
    assert repaired['candidate_id'] == it._repair_candidate(raw, GAPS)['candidate_id']
    assert raw == original


@pytest.mark.parametrize('delta,anchor', [
    ('Change the only paper', None),
    ('Compare 2305.11111 and 2401.22222', None),
    ('Change 2305.11111', '2401.22222')])
def test_no_fabricated_or_ambiguous_anchor(delta, anchor):
    raw = dict(candidate(), corpus_delta=delta, corpus_anchor_id=anchor)
    assert it._repair_candidate(raw, GAPS).get('corpus_anchor_id') == anchor


def test_no_semantic_or_ambiguous_gap_relink():
    raw = dict(candidate(), linked_gap_id='gap_missing')
    assert it._repair_candidate(raw, GAPS)['linked_gap_id'] == 'gap_missing'
    gaps = copy.deepcopy(GAPS)
    gaps['open_gaps'].append(dict(gaps['open_gaps'][0], id='GAP_1'))
    assert it._repair_candidate(candidate(), gaps)['linked_gap_id'] == ' GAP_1 '


def test_confidence_and_counterevidence_reach_both_prompts():
    compact = json.loads(it._compact_gaps(GAPS))['open_gaps'][0]
    assert compact['low_confidence'] is True
    assert compact['counterevidence'] == GAPS['open_gaps'][0]['counterevidence']
    prompt = it._judge_evidence_prompt(candidate(), {}, GAPS['open_gaps'][0])
    assert 'counterevidence' in prompt and 'evidence_tiers' in prompt


def test_pipeline_repair_runs_rubric_and_audits_malformed_and_overflow(monkeypatch):
    seen = []
    context = {'criticism': 'Explain causal novelty'}

    def generate(direction, *args, **kwargs):
        assert direction == 'original direction'
        assert kwargs['revision_context'] == context
        return [candidate(), None, ['malformed'], {'overflow': True}]

    monkeypatch.setattr(it, '_generate_raw_ideas', generate)
    monkeypatch.setattr(it, 'ts_search_by_query', lambda *a, **k: {
        'papers': [{'arxiv_id': '2305.11111', 'title': 'Compression'},
                   {'arxiv_id': '2401.99999', 'title': 'Noise'}],
        'outcome': 'hits', 'query': 'recall compression'})

    def score(idea, prior, gap, lang, **kwargs):
        seen.append(idea)
        assert prior['retrieved_ids'] == ['2305.11111']
        return {'scores': dict.fromkeys(it.RUBRIC_AXES, 5), 'overall': 5,
                'closest_neighbor': '2305.11111', 'mechanism_delta': 'mechanism-level'}

    monkeypatch.setattr(it, '_score_idea', score)
    result = it.generate_ideas('original direction', GAPS, n_ideas=3,
                               revision_context=context)
    assert len(seen) == 1
    assert len(result['accepted']) == 1
    assert len(result['rejected']) == 2
    assert len(result['candidate_audit']) == 4
    assert result['accepted'][0]['cross_domain_inspiration'][0]['arxiv_id'] == '2401.99999'
    assert result['accepted'][0]['scores']['value'] == 4


def test_excluded_only_retrieval_has_no_direct_basis(monkeypatch):
    monkeypatch.setattr(it, 'ts_search_by_query', lambda *a, **k: {
        'papers': [{'arxiv_id': '2401.99999v2', 'title': 'Noise'}],
        'outcome': 'hits', 'query': 'recall'})
    prior = it._novelty_prior_set(candidate(), excluded_ids=['2401.99999'])
    assert prior['novelty_basis'] == 'none'
    assert prior['retrieved_ids'] == []
    assert len(prior['cross_domain_inspiration']) == 1

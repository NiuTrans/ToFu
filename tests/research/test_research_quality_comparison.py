"""Paired comparison is descriptive evidence, not a generated quality score."""
from copy import deepcopy

import pytest

from lib.research.quality_comparison import METRICS, compare_quality

pytestmark = pytest.mark.unit


def pair():
    arm = dict(task_digest='task', corpus_digest='corpus', model='model',
               budget_digest='budget', actual_cost=4, cost_ceiling=5,
               adjudication_kind='independent_evidence_audit', evidence_refs=['audit:1'],
               metrics={key: 0.5 for key in METRICS})
    return dict(pair_id='p1', control=arm, treatment=deepcopy(arm))


def test_matched_differences_do_not_claim_significance():
    row = pair()
    row['treatment']['metrics']['counterevidence_recall'] = 0.75
    result = compare_quality([row])
    assert result['status'] == 'descriptive_only'
    assert result['mean_paired_difference']['counterevidence_recall'] == 0.25
    assert not result['excluded_pairs']


@pytest.mark.parametrize('field,value', [
    ('corpus_digest', 'other'), ('cost_ceiling', 6), ('actual_cost', 9),
    ('actual_cost', float('nan')), ('actual_cost', True),
    ('adjudication_kind', 'model_self_score'), ('evidence_refs', []),
    ('metrics', {}),
])
def test_unmatched_or_unverified_arm_is_excluded(field, value):
    row = pair()
    row['treatment'][field] = value
    result = compare_quality([row])
    assert result['status'] == 'insufficient_evidence'
    assert not result['mean_paired_difference']
    assert result['excluded_pairs']


def test_missing_duplicate_and_bounded_inputs():
    assert compare_quality([])['status'] == 'insufficient_evidence'
    result = compare_quality([pair(), pair(), None])
    assert not result['valid_pairs']
    assert len(result['excluded_pairs']) == 3
    with pytest.raises(ValueError):
        compare_quality([pair()] * 101)


def test_single_pair_uncertainty_is_unknown_instead_of_zero():
    result=compare_quality([pair()])
    assert result['valid_pair_count']==1 and result['submitted_pair_count']==1
    assert result['excluded_pair_count']==0
    assert all(row['standard_error'] is None for row in result['paired_metric_summary'].values())


def test_paired_uncertainty_is_recomputed_and_exclusions_stay_visible():
    rows=[]
    for index,value in enumerate((0.25,0.5,0.75)):
        row=pair();row['pair_id']=f'pair-{index}'
        row['treatment']['metrics']={key:value for key in METRICS};rows.append(row)
    invalid=pair();invalid['pair_id']='excluded';invalid['treatment']['adjudication_kind']='model_self_score'
    rows.append(invalid);before=deepcopy(rows);result=compare_quality(rows)
    assert rows==before
    assert result['submitted_pair_count']==4 and result['valid_pair_count']==3 and result['excluded_pair_count']==1
    for summary in result['paired_metric_summary'].values():
        assert summary['mean']==0 and summary['minimum']==-0.25 and summary['maximum']==0.25
        assert summary['standard_error']==pytest.approx((0.125/6)**0.5)
    assert result['status']=='descriptive_only'


def test_no_valid_pair_has_no_manufactured_summary():
    result=compare_quality([])
    assert result['paired_metric_summary']=={} and result['valid_pair_count']==0


def test_all_supported_pairs_preserve_finite_descriptive_statistics():
    rows=[]
    for index in range(100):
        row=pair();row['pair_id']=str(index)
        row['control']['metrics']={key:0 for key in METRICS}
        row['treatment']['metrics']={key:1 for key in METRICS};rows.append(row)
    result=compare_quality(rows)
    assert result['valid_pair_count']==100
    assert all(summary=={'mean':1,'standard_error':0,'minimum':1,'maximum':1}
        for summary in result['paired_metric_summary'].values())
    assert result['mean_paired_difference']=={key:1 for key in METRICS}


def versioned_pair(identity='p1',treatment='b'*64):
    row=pair();row['pair_id']=identity
    row['control']['system_digest']='a'*64;row['treatment']['system_digest']=treatment
    return row


def test_version_bound_pairs_preserve_exact_generations():
    rows=[versioned_pair('one'),versioned_pair('two')];before=deepcopy(rows)
    result=compare_quality(rows)
    assert result['valid_pair_count']==2 and rows==before
    assert result['system_generations']=={'control':'a'*64,'treatment':'b'*64}
    assert compare_quality([pair()])['system_generations'] is None


@pytest.mark.parametrize('digest',[None,'','not-a-digest','A'*64])
def test_invalid_version_identity_cannot_enter_bound_comparison(digest):
    row=versioned_pair();row['treatment']['system_digest']=digest
    result=compare_quality([row])
    assert result['status']=='insufficient_evidence' and result['valid_pair_count']==0
    assert 'treatment_missing_or_invalid_system_digest' in result['excluded_pairs'][0]['reasons']


def test_legacy_pairs_do_not_inherit_other_pairs_version_binding():
    result=compare_quality([versioned_pair('bound'),{**pair(),'pair_id':'legacy'}])
    assert result['valid_pair_count']==1 and result['excluded_pair_count']==1
    assert result['valid_pairs'][0]['pair_id']=='bound'
    assert 'control_missing_or_invalid_system_digest' in result['excluded_pairs'][0]['reasons']


@pytest.mark.parametrize('reverse',[False,True])
def test_mixed_generations_refuse_aggregate_without_selecting_a_favorable_cohort(reverse):
    rows=[versioned_pair('one'),versioned_pair('two',treatment='c'*64)]
    if reverse:rows.reverse()
    result=compare_quality(rows)
    assert result['status']=='insufficient_evidence' and result['mean_paired_difference']=={}
    assert result['valid_pair_count']==0 and result['excluded_pair_count']==2
    assert all(row['reasons']==['mixed_system_generations'] for row in result['excluded_pairs'])


def test_changed_control_generation_is_visible_in_exclusion_diagnostics():
    rows=[versioned_pair('one'),versioned_pair('two')]
    rows[1]['control']['system_digest']='c'*64
    result=compare_quality(rows)
    assert not result['valid_pairs']
    assert [row['system_digests']['control'] for row in result['excluded_pairs']]==['a'*64,'c'*64]
    assert [row['index'] for row in result['excluded_pairs']]==[0,1]


@pytest.mark.parametrize('invalid_duplicate', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_all_duplicate_id_copies_are_excluded_without_order_selection(invalid_duplicate, reverse):
    first = pair()
    first['treatment']['metrics'] = {key: 1 for key in METRICS}
    duplicate = pair()
    duplicate['treatment']['metrics'] = {key: 0 for key in METRICS}
    if invalid_duplicate:
        duplicate['treatment']['adjudication_kind'] = 'model_self_score'
    independent = pair()
    independent['pair_id'] = 'independent'
    independent['treatment']['metrics'] = {key: 0.75 for key in METRICS}
    rows = [first, duplicate, independent]
    if reverse:
        rows.reverse()
    before = deepcopy(rows)
    result = compare_quality(rows)
    assert rows == before
    assert [row['pair_id'] for row in result['valid_pairs']] == ['independent']
    assert result['mean_paired_difference'] == {key: 0.25 for key in METRICS}
    assert result['valid_pair_count'] == 1 and result['excluded_pair_count'] == 2
    assert all('missing_or_duplicate_pair_id' in row['reasons'] for row in result['excluded_pairs'])
    assert all(row['standard_error'] is None for row in result['paired_metric_summary'].values())

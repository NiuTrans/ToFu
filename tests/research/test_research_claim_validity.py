"""Scientific support is dependent evidence, not successful execution."""
import copy

import pytest

from lib.research.action import apply_action_result
from lib.research.program import normalize_program_fields, readiness
from lib.research.workspace import empty_workspace

pytestmark = pytest.mark.unit


def _result(outcome='supported'):
    workspace = empty_workspace('Matched experiment')
    workspace['hypothesis'] = 'Intervention improves held-out accuracy'
    return apply_action_result(
        action='experiment', workspace=workspace, task_id='t1', bindings=[],
        payload={'run': {'id': 'r1', 'status': 'passed'}, 'claims': [
            {'id': 'c1', 'text': 'Improves accuracy', 'status': 'supported',
             'evidence_refs': ['r1']}]},
        receipts=[{'tool': 'run_command', 'status': 'done',
                   'artifact_ref': 'evidence:1', 'result_digest': 'abc',
                   'result_excerpt': '{"scientific_outcome":"' + outcome + '"}'}])


def test_support_requires_observed_outcome_not_execution_pass():
    result = _result('inconclusive')
    assert result['runs'][0]['execution_status'] == 'completed'
    assert result['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert result['claims'][0]['status'] == 'contested'
    assert not readiness(result)['ready']


@pytest.mark.parametrize('change', ['hypothesis', 'protocol', 'evidence', 'claim'])
def test_dependency_changes_contest_previously_supported_claim(change):
    result = _result()
    assert result['claims'][0]['status'] == 'supported'
    assert normalize_program_fields(result)['claims'][0]['validity'] == 'current'
    changed = copy.deepcopy(result)
    if change == 'hypothesis':
        changed['hypothesis'] = 'Different hypothesis'
    elif change == 'protocol':
        changed['protocol']['baseline'] = 'Different comparator'
    elif change == 'claim':
        changed['claims'][0]['text'] = 'Different assertion'
    else:
        changed['runs'][0]['tool_receipts'][0]['result_digest'] = 'edited'
    normalized = normalize_program_fields(changed)
    assert normalized['claims'][0]['status'] == 'contested'
    assert normalized['claims'][0]['evidence_refs'] == ['r1']
    assert not readiness(changed)['ready']


def test_legacy_pass_is_preserved_but_never_promoted_to_scientific_support():
    normalized = normalize_program_fields({'runs': [
        {'id': 'old', 'status': 'passed', 'artifact_ref': 'old:data'}],
        'claims': [{'id': 'old-claim', 'text': 'Legacy assertion',
                    'status': 'supported', 'evidence_refs': ['old']}]})
    assert normalized['runs'][0]['status'] == 'passed'
    assert normalized['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert normalized['runs'][0]['validity'] == 'unverified'
    assert normalized['claims'][0]['status'] == 'contested'


def test_refutation_preserved_and_model_cannot_mutate_frozen_protocol():
    result = _result('refuted')
    assert result['runs'][0]['scientific_outcome'] == 'refuted'
    assert result['claims'][0]['status'] == 'contested'
    updated = apply_action_result(action='experiment', workspace=result,
        payload={'protocol': {'baseline': 'invented'}, 'run': {
            'scientific_outcome': 'supported'}}, receipts=[], task_id='t2', bindings=[])
    assert updated['protocol'] == result['protocol']
    assert updated['runs'][-1]['scientific_outcome'] == 'inconclusive'


def _scoped_result(outcome):
    from lib.research.program import claim_evidence_digest
    result = _result(outcome)
    claim = result['claims'][0]
    claim.update(status='supported', outcome_scope=outcome)
    claim['evidence_digest'] = claim_evidence_digest(claim, result['runs'])
    result.update(normalize_program_fields(result))
    assert result['claims'][0]['validity'] == 'current'
    return result


@pytest.mark.parametrize('scope', ['supported', 'refuted'])
def test_opposite_current_receipts_contest_a_cherry_picked_claim(scope):
    from lib.research.program import claim_evidence_digest
    result = _scoped_result(scope)
    opposing = copy.deepcopy(result['runs'][0])
    opposing.update(id='r2', task_id='t2', artifact_ref='evidence:2', artifact_refs=['evidence:2'],
                    scientific_outcome='refuted' if scope == 'supported' else 'supported')
    opposing['tool_receipts'][0].update(result_digest='opposite',
        result_excerpt='{"scientific_outcome":"' + opposing['scientific_outcome'] + '"}')
    result['runs'].append(opposing)
    # Re-hashing only the selected evidence cannot erase the opposing receipt.
    result['claims'][0]['evidence_digest'] = claim_evidence_digest(result['claims'][0], result['runs'])
    normalized = normalize_program_fields(result)
    assert normalized['decision']['status'] == 'refine'
    assert normalized['claims'][0]['status'] == 'contested'
    assert normalized['claims'][0]['validity'] != 'current'
    assert 'conflicting' in normalized['claims'][0]['validity_reason']
    assert normalized['claims'][0]['evidence_refs'] == ['r1']


@pytest.mark.parametrize('excluded', ['stale', 'running', 'no_receipt'])
def test_unproven_or_out_of_scope_opposition_does_not_invent_a_conflict(excluded):
    result = _scoped_result('supported')
    opposing = copy.deepcopy(result['runs'][0])
    opposing.update(id='r2', artifact_ref='evidence:2', artifact_refs=['evidence:2'], scientific_outcome='refuted')
    if excluded == 'stale':
        opposing['protocol_digest'] = 'different frozen protocol'
    elif excluded == 'running':
        opposing['execution_status'] = 'running'
    elif excluded == 'no_receipt':
        opposing['tool_receipts'] = []
    else:
        opposing['scientific_outcome'] = 'inconclusive'
    result['runs'].append(opposing)
    normalized = normalize_program_fields(result)
    assert normalized['claims'][0]['status'] == 'supported'
    assert normalized['claims'][0]['validity'] == 'current'
    assert normalized['decision']['status'] != 'refine'


def test_current_inconclusive_receipt_prevents_cherry_picked_support_without_inventing_refutation():
    result=_scoped_result('supported')
    uncertain=copy.deepcopy(result['runs'][0])
    uncertain.update(id='uncertain',task_id='uncertain-task',scientific_outcome='inconclusive')
    uncertain['tool_receipts'][0].update(result_digest='uncertain-observation',
        result_excerpt='{"scientific_outcome":"inconclusive"}')
    result['runs'].append(uncertain)
    normalized=normalize_program_fields(result)
    assert normalized['claims'][0]['status']=='contested'
    assert normalized['claims'][0]['validity']!='current'
    assert normalized['decision']['status']=='inconclusive'
    assert 'inconclusive' in normalized['claims'][0]['validity_reason']

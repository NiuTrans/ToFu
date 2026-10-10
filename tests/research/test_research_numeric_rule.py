"""Precommitted quantitative rules bind measured effects to conclusions."""
from copy import deepcopy
import json
import pytest
from jsonschema import Draft202012Validator
from pathlib import Path
from lib.research.observation_audit import measured_outcome, normalize_decision_rule, verify_observations
from lib.research.program import dependency_digests, normalize_program_fields, readiness, scientific_dependency_digest
from tests.research.test_research_decisions import result_workspace
from tests.support.research_observations import observation_payload
from tests.research.test_research_history import DIRECTION, draft_with_runs, observation, save
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit
RULE={'direction':'higher','minimum_effect':0.0,'uncertainty_multiplier':2.0}

@pytest.mark.parametrize('direction,mean,se,threshold,expected', [
    ('higher',.2,.01,0,'supported'), ('higher',-.2,.01,0,'refuted'),
    ('lower',-.2,.01,0,'supported'), ('lower',.2,.01,0,'refuted'),
    ('higher',0,0,0,'inconclusive'), ('higher',.2,.1,0,'inconclusive'),
    ('higher',-.2,.1,0,'inconclusive'), ('higher',-.01,.01,-.05,'supported'),
    ('higher',-.1,.01,-.05,'refuted'), ('higher',.2,.01,.3,'refuted'),
    ('higher',.2,1,0,'inconclusive')])
def test_oriented_effect_and_precommitted_margin(direction,mean,se,threshold,expected):
    assert measured_outcome(mean,se,dict(RULE,direction=direction,minimum_effect=threshold))==expected

@pytest.mark.parametrize('rule', [None, {}, dict(RULE,direction='accuracy'), dict(RULE,minimum_effect=True),
    dict(RULE,minimum_effect=float('inf')),dict(RULE,uncertainty_multiplier=0),
    dict(RULE,uncertainty_multiplier=-1),dict(RULE,uncertainty_multiplier=float('nan')),
    dict(RULE,uncertainty_multiplier=10**400),dict(RULE,extra='guess')])
def test_invalid_rules_are_rejected_not_silently_removed(rule):
    with pytest.raises(ValueError):normalize_decision_rule(rule)
    workspace=result_workspace();workspace['protocol']['decision_rule']=rule
    with pytest.raises(ValueError):normalize_program_fields(workspace)

@pytest.mark.parametrize('direction,outcome,expected', [('higher','supported','retain'),
    ('higher','refuted','refute'),('lower','refuted','inconclusive'),('lower','supported','inconclusive')])
def test_decision_and_manuscript_compute_from_frozen_rule(direction,outcome,expected):
    workspace=result_workspace(outcome)
    workspace['protocol']['decision_rule']=dict(RULE,direction=direction)
    workspace['runs'][0].update(dependency_digests(workspace))
    workspace.update(normalize_program_fields(workspace))
    assert workspace['decision']['status']==expected
    checks=[c['ok'] for c in workspace['manuscript_audit']['checks'] if c['id']!='claim_traceability']
    assert all(checks)==(expected!='inconclusive')
    schema=json.loads(Path('contracts/research_program_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(workspace)


def test_original_negative_measurements_cannot_self_report_support():
    workspace=result_workspace('supported');workspace['protocol']['decision_rule']=RULE.copy()
    workspace['runs'][0].update(dependency_digests(workspace))
    payload=observation_payload(workspace['protocol'],'artifact:1','refuted')
    payload['scientific_outcome']='supported'
    workspace['runs'][0]['tool_receipts'][0]['result_excerpt']=json.dumps(payload)
    assert not verify_observations(payload,workspace['protocol'])['uncertainty']
    workspace.update(normalize_program_fields(workspace))
    assert workspace['decision']['status']=='inconclusive'
    assert not workspace['manuscript_audit']['passed']


def test_protocol_edit_stales_evidence_instead_of_reinterpreting_old_measurement():
    workspace=result_workspace('supported');workspace['protocol']['decision_rule']=RULE.copy()
    workspace['runs'][0].update(dependency_digests(workspace));workspace.update(normalize_program_fields(workspace))
    before=scientific_dependency_digest(workspace)
    workspace['protocol']['decision_rule']['minimum_effect']=.5
    workspace.update(normalize_program_fields(workspace))
    assert workspace['runs'][0]['validity']=='stale'
    assert workspace['decision']['status']=='inconclusive'
    assert scientific_dependency_digest(workspace)!=before


def test_matching_outcome_without_measurements_cannot_pass_new_numeric_policy():
    workspace=result_workspace('supported');workspace['protocol']['decision_rule']=RULE.copy()
    workspace['runs'][0].update(dependency_digests(workspace))
    workspace['runs'][0]['tool_receipts'][0]['result_excerpt']='{"scientific_outcome":"supported"}'
    workspace.update(normalize_program_fields(workspace))
    assert workspace['decision']['status']=='inconclusive'


def test_numeric_rule_survives_archive_and_preserves_original_false_report(storage):
    draft=draft_with_runs(24);draft['protocol']['decision_rule']=RULE.copy()
    draft['runs']=[observation(draft,i) for i in range(24)]
    payload=observation_payload(draft['protocol'],'metrics/0.json','refuted')
    payload['scientific_outcome']='supported'
    draft['runs'][0]['tool_receipts'][0]['result_excerpt']=json.dumps(payload)
    reviewed=scientific_dependency_digest(draft)
    saved=save(draft)
    assert saved['history']['outcomes']=={'supported':7,'refuted':0,'inconclusive':1}
    assert saved['history']['audit_failures']['uncertainty']==1
    assert saved['decision']['status']=='inconclusive'
    assert scientific_dependency_digest(saved)==reviewed

@pytest.mark.parametrize('direction,threshold', [('lower',0), ('higher',-.3)])
def test_lower_metric_and_explicit_noninferiority_can_support_measured_negative_difference(direction,threshold):
    workspace=result_workspace('supported')
    workspace['protocol']['decision_rule']=dict(RULE,direction=direction,minimum_effect=threshold)
    workspace['runs'][0].update(dependency_digests(workspace))
    payload=observation_payload(workspace['protocol'],'artifact:1','refuted')
    payload['scientific_outcome']='supported'
    workspace['runs'][0]['tool_receipts'][0]['result_excerpt']=json.dumps(payload)
    assert all(verify_observations(payload,workspace['protocol']).values())
    workspace.update(normalize_program_fields(workspace))
    assert workspace['decision']['status']=='retain'


def test_nonfinite_margin_cannot_turn_into_decisive_outcome():
    assert measured_outcome(1e308,1e308,dict(RULE,uncertainty_multiplier=10))=='inconclusive'


def test_equivalent_numeric_encodings_do_not_stale_frozen_protocol():
    workspace=result_workspace();workspace['protocol']['decision_rule']=dict(RULE,minimum_effect=-0.0,uncertainty_multiplier=2)
    before=dependency_digests(workspace)
    workspace['protocol']['decision_rule']=dict(RULE,minimum_effect=0,uncertainty_multiplier=2.0)
    assert dependency_digests(workspace)==before


@pytest.mark.parametrize('outcome',['supported','inconclusive'])
def test_tiny_values_do_not_square_uncertainty_to_zero_and_invent_support(outcome):
    workspace=result_workspace('supported');protocol=workspace['protocol'];protocol['decision_rule']=RULE.copy()
    payload=observation_payload(protocol,'artifact:1','supported')
    sample=payload['scientific_observations']['samples']
    sample.update(method_values=[-1e-200,1e-200,1e-200],baseline_values=[0,0,0],
                  mean_difference=1e-200/3,standard_error=0)
    payload['scientific_outcome']=outcome
    # Squaring 1e-200 underflows; zero reported SE is invalid in either outcome.
    assert not verify_observations(payload,protocol)['uncertainty']


def test_command_result_cannot_retune_frozen_threshold_after_observing_failure():
    from tests.research.test_research_command_observations import receipt
    from lib.research.action import apply_action_result
    workspace=result_workspace('supported');workspace['runs']=[]
    workspace['protocol']['decision_rule']=RULE.copy()
    payload=observation_payload(workspace['protocol'],'artifact:1','refuted')
    payload['scientific_outcome']='supported'
    observed,_=receipt(json.dumps(payload))
    retuned=deepcopy(workspace['protocol']);retuned['decision_rule']['minimum_effect']=-1
    result=apply_action_result(action='experiment',workspace=workspace,task_id='measured',bindings=[],
        payload={'protocol':retuned,'run':{'id':'metrics','scientific_outcome':'supported'}},receipts=[observed])
    assert result['protocol']['decision_rule']==RULE
    assert result['runs'][0]['execution_status']=='completed'
    assert result['decision']['status']=='inconclusive'
    assert not result['manuscript_audit']['passed']


def test_measurement_scope_round_trip_and_dependency_binding(storage):
    from lib.research.workspace import save_workspace,load_workspace
    workspace=result_workspace()
    context=observation_payload(workspace['protocol'],'metrics')['scientific_observations']['comparison']['method']
    scope={key:context[key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    workspace['protocol']['measurement_scope']=scope
    original=save_workspace(DIRECTION,'en',workspace,expected_revision=0,user_id=1)
    restored=load_workspace(DIRECTION,'en',user_id=1)
    assert restored['protocol']['measurement_scope']==scope
    digest=scientific_dependency_digest(restored)
    draft=deepcopy(restored);draft['protocol']['measurement_scope']['budget']+=1
    changed=save_workspace(DIRECTION,'en',draft,expected_revision=original['revision'],user_id=1)
    assert scientific_dependency_digest(changed)!=digest

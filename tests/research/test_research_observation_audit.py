"""Scientific audit flags never replace computable paired observations."""
from copy import deepcopy
import json
import pytest
from lib.research.observation_audit import verify_observations, partition_digest
from lib.research.decisions import manuscript_audit, AUDIT_CHECKS
from tests.support.research_observations import observation_payload

pytestmark=pytest.mark.unit
PROTOCOL={'baseline':'reference','dataset':'held-out','primary_metric':'accuracy','random_seeds':[1,2,3]}


def test_self_attestation_alone_cannot_pass():
    payload={'scientific_audit':{key:{'status':'verified','evidence_refs':['metrics']} for key in AUDIT_CHECKS}}
    run={'id':'r','execution_status':'completed','validity':'current','artifact_ref':'metrics','scientific_outcome':'supported',
         'tool_receipts':[{'status':'done','result_digest':'receipt','result_excerpt':json.dumps(payload)}]}
    assert not manuscript_audit([run],[],protocol=PROTOCOL)['passed']
    assert not any(verify_observations(payload,PROTOCOL).values())


def test_consistency_is_computed_without_trusting_verification_labels():
    payload=observation_payload(PROTOCOL,'metrics')
    for check in payload['scientific_audit'].values():check['status']='unverified'
    assert all(verify_observations(payload,PROTOCOL).values())


@pytest.mark.parametrize('change,failed',[
 ('overlap','validation_split'),('gap','validation_split'),('missing_test','validation_split'),
 ('different_dataset','baseline_fairness'),('different_metric','baseline_fairness'),
 ('different_budget','baseline_fairness'),('training_score','validation_split'),
 ('missing_seed','seeds'),('duplicate_seed','seeds'),('extra_seed','seeds'),
 ('wrong_mean','uncertainty'),('zero_error','uncertainty'),('infinite','uncertainty'),
 ('boolean','uncertainty'),('huge_integer','uncertainty'),('mismatched_vectors','uncertainty')])
def test_invalid_observations_fail_the_relevant_gate(change,failed):
    payload=observation_payload(PROTOCOL,'metrics');obs=payload['scientific_observations'];sample=obs['samples']
    if change=='overlap':obs['partitions']['test']=[[799,1000]]
    elif change=='gap':obs['partitions']['test']=[[801,1000]]
    elif change=='missing_test':obs['partitions']['test']=[]
    elif change=='different_dataset':obs['comparison']['baseline']['dataset_digest']='e'*64
    elif change=='different_metric':obs['comparison']['baseline']['metric']='training accuracy'
    elif change=='different_budget':obs['comparison']['method']['budget']=200
    elif change=='training_score':obs['comparison']['method']['evaluation_partition']='train'
    elif change=='missing_seed':sample['seeds']=[1,2]
    elif change=='duplicate_seed':sample['seeds']=[1,1,3]
    elif change=='extra_seed':sample['seeds']=[1,2,3,4]
    elif change=='wrong_mean':sample['mean_difference']=0.99
    elif change=='zero_error':sample['standard_error']=0
    elif change=='infinite':sample['method_values'][0]=float('inf')
    elif change=='boolean':sample['method_values'][0]=True
    elif change=='huge_integer':sample['method_values'][0]=10**400
    else:sample['baseline_values']=[0.5]
    assert not verify_observations(payload,PROTOCOL)[failed]


def test_interval_proof_covers_large_datasets_without_enumerating_records():
    payload=observation_payload(PROTOCOL,'metrics');obs=payload['scientific_observations']
    obs['partitions']={'dataset_size':10**9,'train':[[0,600000000]],
                      'validation':[[600000000,800000000]],'test':[[800000000,10**9]]}
    for context in obs['comparison'].values():context['split_digest']=partition_digest(obs['partitions'])
    assert all(verify_observations(payload,PROTOCOL).values())


def test_schema_and_receipt_budget_match_observation_format():
    from pathlib import Path
    import jsonschema
    schema=json.loads((Path(__file__).resolve().parents[2]/'contracts/research_program_v1.schema.json').read_text())
    payload=observation_payload(PROTOCOL,'metrics')
    jsonschema.validate(payload['scientific_observations'],{'$defs':schema['$defs'],'$ref':'#/$defs/scientificObservations'})
    assert len(json.dumps(payload))<4000


def test_agent_context_exposes_the_same_machine_contract_without_changing_workspace(monkeypatch):
    import lib.research.action as action
    from lib.research.workspace import empty_workspace
    from lib.research.observation_audit import observation_contract
    workspace=empty_workspace('Research');before=deepcopy(workspace)
    monkeypatch.setattr(action,'_research_context',lambda *_:{})
    messages=action._messages({'action':'experiment','direction':'Research','lang':'en','user_id':1},workspace,[])
    payload=json.loads(messages[1]['content'])
    assert payload['scientific_observation_contract']==observation_contract()
    assert workspace==before


@pytest.mark.parametrize('link', ['foreign', 'missing'])
def test_computed_observations_need_receipt_linked_evidence(link):
    payload=observation_payload(PROTOCOL,'metrics')
    for entry in payload['scientific_audit'].values():entry['evidence_refs']=['foreign'] if link=='foreign' else []
    run={'id':'r','execution_status':'completed','validity':'current','artifact_ref':'metrics','scientific_outcome':'supported',
         'tool_receipts':[{'status':'done','result_digest':'receipt','result_excerpt':json.dumps(payload)}]}
    audit=manuscript_audit([run],[{'status':'supported','validity':'current'}],protocol=PROTOCOL)
    assert not audit['passed']
    assert not any(check['ok'] for check in audit['checks'] if check['id'] in AUDIT_CHECKS)


def test_old_status_only_manuscript_is_neither_ready_nor_published_current():
    from pathlib import Path
    from lib.research.program import readiness
    old=json.loads((Path(__file__).resolve().parents[1]/'fixtures/research/legacy-status-only-manuscript.json').read_text())
    assert old['publication']['status']=='published'
    result=readiness(old)
    assert not result['ready']
    assert not result['published_current']


def test_owner_baseline_cannot_be_substituted_with_a_weaker_named_system():
    payload=observation_payload(PROTOCOL,'metrics')
    payload['scientific_observations']['comparison']['baseline']['system']='weaker reference'
    assert not verify_observations(payload,PROTOCOL)['baseline_fairness']


def test_named_baseline_self_comparison_can_measure_calibration_noise():
    payload=observation_payload(PROTOCOL,'metrics')
    payload['scientific_observations']['comparison']['method']['system']=PROTOCOL['baseline']
    assert all(verify_observations(payload,PROTOCOL).values())


@pytest.mark.parametrize('payload', [None, [], {'scientific_observations':None},
    {'scientific_observations':{'format':'unknown'}}, {'scientific_observations':{'format':'tofu.scientific-observations/v1'}}])
def test_missing_or_unknown_observation_contract_remains_unverified(payload):
    assert not any(verify_observations(payload,PROTOCOL).values())

@pytest.mark.parametrize('complete', [False, True])
def test_one_complete_observation_must_prove_every_gate(complete):
    first=observation_payload(PROTOCOL,'metrics')
    second=deepcopy(first)
    first['scientific_observations']['samples']['mean_difference']=0.99
    second['scientific_observations']['comparison']['method']['budget']=200
    payloads=[first,second]
    if complete:payloads.append(observation_payload(PROTOCOL,'metrics'))
    run={'id':'r','execution_status':'completed','validity':'current','artifact_ref':'metrics',
         'scientific_outcome':'supported',
         'tool_receipts':[{'status':'done','result_digest':'receipt','result_excerpt':json.dumps(p)} for p in payloads]}
    audit=manuscript_audit([run],[{'status':'supported','validity':'current'}],protocol=PROTOCOL)
    assert audit['passed'] is complete


@pytest.mark.parametrize('field', ['dataset_digest','split_digest','budget_unit','budget'])
def test_matching_reports_cannot_substitute_frozen_measurement_scope(field):
    payload=observation_payload(PROTOCOL,'metrics')
    comparison=payload['scientific_observations']['comparison']
    frozen={key:comparison['method'][key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    protocol={**PROTOCOL,'measurement_scope':frozen}
    assert all(verify_observations(payload,protocol).values())
    frozen[field]=({'dataset_digest':'f'*64,'split_digest':'e'*64,'budget_unit':'different-unit','budget':123456}[field])
    checks=verify_observations(payload,protocol)
    assert not checks['baseline_fairness']
    if field.endswith('digest'):assert not checks['validation_split']


@pytest.mark.parametrize('scope', [None,{}, {'budget':True}, {'dataset_digest':'a'*64,'split_digest':'b'*64,'budget_unit':'cpu','budget':float('inf')}])
def test_invalid_measurement_scope_refuses_normalization(scope):
    from lib.research.program import normalize_program_fields
    with pytest.raises(ValueError):normalize_program_fields({'protocol':{**PROTOCOL,'measurement_scope':scope}})


@pytest.mark.parametrize('substitute',['none','method','baseline','missing','swapped'])
def test_frozen_system_generations_cannot_be_substituted(substitute):
    from lib.research.program import normalize_program_fields
    from lib.research.observation_audit import observation_contract
    import jsonschema
    payload=observation_payload(PROTOCOL,'metrics');comparison=payload['scientific_observations']['comparison']
    scope={key:comparison['method'][key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    scope.update(candidate_digest='a'*64,baseline_digest='b'*64)
    protocol=normalize_program_fields({'protocol':{**PROTOCOL,'measurement_scope':scope}})['protocol']
    assert protocol['measurement_scope']==scope
    comparison['method']['system_digest']='a'*64;comparison['baseline']['system_digest']='b'*64
    jsonschema.validate(payload['scientific_observations'],observation_contract())
    if substitute in ('method','baseline'):comparison[substitute]['system_digest']='f'*64
    elif substitute=='missing':comparison['method'].pop('system_digest')
    elif substitute=='swapped':
        comparison['method']['system_digest']='b'*64;comparison['baseline']['system_digest']='a'*64
    checks=verify_observations(payload,protocol)
    assert checks['validation_split']
    assert checks['baseline_fairness']==(substitute=='none')


@pytest.mark.parametrize('change',['candidate_only','baseline_only','invalid_candidate','invalid_baseline'])
def test_generation_scope_requires_complete_valid_pair(change):
    from lib.research.program import normalize_program_fields
    payload=observation_payload(PROTOCOL,'metrics')
    scope={key:payload['scientific_observations']['comparison']['method'][key]
        for key in ('dataset_digest','split_digest','budget_unit','budget')}
    scope.update(candidate_digest='a'*64,baseline_digest='b'*64)
    if change=='candidate_only':scope.pop('baseline_digest')
    elif change=='baseline_only':scope.pop('candidate_digest')
    else:scope[change.replace('invalid_','')+'_digest']='invalid'
    with pytest.raises(ValueError):normalize_program_fields({'protocol':{**PROTOCOL,'measurement_scope':scope}})


@pytest.mark.parametrize('system',['method','baseline'])
def test_invalid_optional_report_digest_remains_unverified_without_frozen_generation(system):
    payload=observation_payload(PROTOCOL,'metrics')
    payload['scientific_observations']['comparison'][system]['system_digest']='invalid'
    assert not verify_observations(payload,PROTOCOL)['baseline_fairness']


def test_frozen_generation_with_missing_comparison_refuses_without_exception():
    payload=observation_payload(PROTOCOL,'metrics');comparison=payload['scientific_observations']['comparison']
    scope={key:comparison['method'][key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    scope.update(candidate_digest='a'*64,baseline_digest='b'*64)
    payload['scientific_observations']['comparison']={}
    assert not verify_observations(payload,{**PROTOCOL,'measurement_scope':scope})['baseline_fairness']


@pytest.mark.parametrize('seeds',[[1.9,2.1],[1.0,2.0],['1','2'],[True,1,2],'123',list(range(33)),None])
def test_numeric_protocol_preserves_exact_seed_admission(seeds):
    from lib.research.program import normalize_program_fields
    frozen={**PROTOCOL,'random_seeds':seeds,'decision_rule':{'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}}
    with pytest.raises(ValueError,match='exact integer list'):
        normalize_program_fields({'protocol':frozen})


def test_numeric_seed_bound_preserves_all_entries_in_order():
    from lib.research.program import normalize_program_fields
    seeds=list(range(31,-1,-1))
    normalized=normalize_program_fields({'protocol':{**PROTOCOL,'random_seeds':seeds,
        'decision_rule':{'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}}})
    assert normalized['protocol']['random_seeds']==seeds


def test_protocol_specific_observation_schema_requires_frozen_generation_reports():
    from lib.research.observation_audit import observation_contract
    import jsonschema
    payload=observation_payload(PROTOCOL,'metrics')['scientific_observations']
    frozen={**PROTOCOL,'measurement_scope':{'candidate_digest':'a'*64,'baseline_digest':'b'*64}}
    contract=observation_contract(frozen)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(payload,contract)
    payload['comparison']['method']['system_digest']='a'*64
    payload['comparison']['baseline']['system_digest']='b'*64
    jsonschema.validate(payload,contract)
    # Contract projections never strengthen an unrelated legacy protocol.
    for system in ('method','baseline'):
        assert 'system_digest' not in observation_contract()['properties']['comparison']['properties'][system]['required']


@pytest.mark.parametrize('blank',['',' ','\t\n','\u3000'])
@pytest.mark.parametrize('field',['system','budget_unit'])
def test_blank_comparison_identity_or_budget_unit_cannot_pass(blank,field):
    from lib.research.observation_audit import observation_contract
    import jsonschema
    payload=observation_payload(PROTOCOL,'metrics')
    comparison=payload['scientific_observations']['comparison']
    comparison['method'][field]=blank
    if field=='budget_unit':comparison['baseline'][field]=blank
    assert not verify_observations(payload,PROTOCOL)['baseline_fairness']
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload['scientific_observations'],observation_contract())


@pytest.mark.parametrize('blank',['',' ','\t\n','\u3000'])
def test_blank_baseline_cannot_pass_even_when_raw_protocol_matches(blank):
    raw_protocol={**PROTOCOL,'baseline':blank}
    payload=observation_payload(raw_protocol,'metrics')
    assert not verify_observations(payload,raw_protocol)['baseline_fairness']


@pytest.mark.parametrize('mixed', [False, True])
def test_finite_paired_statistics_survive_intermediate_overflow(mixed):
    payload = observation_payload(PROTOCOL, 'metrics')
    samples = payload['scientific_observations']['samples']
    value = 1.79e308 if mixed else 1e308
    samples['method_values'] = [value, -value, -value] if mixed else [value] * 3
    samples['baseline_values'] = [0.0] * 3
    samples['mean_difference'] = -value / 3 if mixed else value
    samples['standard_error'] = value * (2 / 3) if mixed else 0.0
    assert all(verify_observations(payload, PROTOCOL).values())


@pytest.mark.parametrize('case', ['ordered', 'reordered', 'missing', 'duplicate', 'foreign', 'wrong_statistics', 'legacy_fields'])
def test_identified_observations_pair_each_system_by_seed(case):
    from lib.research.observation_audit import observation_contract
    from jsonschema import Draft202012Validator
    payload = observation_payload(PROTOCOL, 'metrics')
    evidence = payload['scientific_observations']
    evidence['format'] = 'tofu.scientific-observations/v2'
    samples = evidence['samples']
    samples.update(method_seeds=[1,2,3], baseline_seeds=[1,2,3],
                   method_values=[3.0,5.0,7.0], baseline_values=[2.0,4.0,6.0],
                   mean_difference=1.0, standard_error=0.0)
    if case in ('reordered', 'wrong_statistics'):
        samples['baseline_seeds'] = [3,1,2]
        samples['baseline_values'] = [6.0,2.0,4.0]
    if case == 'missing': samples.pop('method_seeds')
    elif case == 'duplicate': samples['method_seeds'] = [1,1,3]
    elif case == 'foreign': samples['method_seeds'] = [1,2,4]
    elif case == 'wrong_statistics': samples['standard_error'] = 2.0
    elif case == 'legacy_fields': evidence['format'] = 'tofu.scientific-observations/v1'
    valid = case in ('ordered', 'reordered')
    assert all(verify_observations(payload, PROTOCOL).values()) is valid
    if case in ('ordered', 'reordered', 'missing', 'legacy_fields'):
        assert Draft202012Validator(observation_contract()).is_valid(evidence) is valid


def test_ambiguous_scientific_receipt_cannot_be_replaced_by_later_valid_measurement():
    from lib.research.science_reduction import reduce_scientific_receipts
    payload = observation_payload(PROTOCOL, 'metrics')
    valid = json.dumps(payload)
    ambiguous = '{"scientific_outcome":"refuted",' + valid[1:]
    protocol = {**PROTOCOL, 'decision_rule': {'direction': 'higher', 'minimum_effect': 0, 'uncertainty_multiplier': 2}}
    rows = [{'status':'done', 'result_digest':str(index), 'result_excerpt':raw}
            for index, raw in enumerate([ambiguous, valid])]
    result = reduce_scientific_receipts(rows, expected_outcome='supported', protocol=protocol)
    assert not result['outcome_consistent']


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}'])
def test_scientific_decoder_refuses_duplicate_keys_at_every_depth(raw):
    from lib.research.observation_audit import decode_observation_json
    with pytest.raises(ValueError, match='duplicate'):
        decode_observation_json(raw)


def test_excessive_scientific_json_nesting_is_an_invalid_receipt_not_a_crash():
    from lib.research.observation_audit import decode_observation_json
    from lib.research.science_reduction import reduce_scientific_receipts
    depth = 10000
    raw = '{"scientific_outcome":"supported","scientific_observations":' + '[' * depth + '0' + ']' * depth + '}'
    with pytest.raises(ValueError):
        decode_observation_json(raw)
    result = reduce_scientific_receipts([{'status':'done', 'result_digest':'nested', 'result_excerpt':raw}],
                                       expected_outcome='supported', protocol=PROTOCOL)
    assert not result['outcome_consistent']


@pytest.mark.parametrize('wrapper', ['array', 'object'])
def test_malformed_scientific_envelope_cannot_be_hidden_by_later_valid_receipt(wrapper):
    from lib.research.science_reduction import reduce_scientific_receipts
    payload = observation_payload(PROTOCOL, 'metrics')
    malformed = [payload] if wrapper == 'array' else {'data': payload}
    protocol = {**PROTOCOL, 'decision_rule': {'direction':'higher', 'minimum_effect':0, 'uncertainty_multiplier':2}}
    receipts = [{'status':'done', 'result_digest':str(index), 'result_excerpt':json.dumps(value)}
                for index, value in enumerate([malformed, payload])]
    assert not reduce_scientific_receipts(receipts, expected_outcome='supported', protocol=protocol)['outcome_consistent']


@pytest.mark.parametrize('count,valid', [(2,True), (32,True), (33,False)])
def test_direct_observation_audit_enforces_canonical_seed_population_bounds(count, valid):
    protocol = {**PROTOCOL, 'random_seeds':list(range(count))}
    payload = observation_payload(protocol, 'metrics')
    assert all(verify_observations(payload, protocol).values()) is valid


def test_observation_contract_callers_cannot_mutate_cached_bounds_or_requirements():
    from lib.research.observation_audit import observation_contract
    contract = observation_contract()
    original = deepcopy(contract)
    contract['properties']['samples']['properties']['seeds']['maxItems'] = 1000000
    contract['properties']['comparison']['properties']['method']['required'].clear()
    assert observation_contract() == original
    protocol = {**PROTOCOL, 'random_seeds':list(range(33))}
    assert not all(verify_observations(observation_payload(protocol, 'metrics'), protocol).values())


@pytest.mark.parametrize('field', ['dataset_digest','split_digest','budget_unit','budget','system_digest'])
@pytest.mark.parametrize('system', ['method','baseline'])
def test_projected_observation_contract_binds_every_frozen_comparison_field(field, system):
    from lib.research.observation_audit import observation_contract
    from jsonschema import Draft202012Validator
    payload = observation_payload(PROTOCOL, 'metrics')['scientific_observations']
    scope = {key:payload['comparison']['method'][key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    scope.update(candidate_digest='a'*64, baseline_digest='b'*64)
    payload['comparison']['method']['system_digest'] = scope['candidate_digest']
    payload['comparison']['baseline']['system_digest'] = scope['baseline_digest']
    contract = observation_contract({**PROTOCOL, 'measurement_scope':scope})
    validator = Draft202012Validator(contract)
    assert validator.is_valid(payload)
    changed = deepcopy(payload)
    changed['comparison'][system][field] = 101 if field == 'budget' else ('c'*64 if field != 'budget_unit' else 'other-unit')
    assert not validator.is_valid(changed)
    assert Draft202012Validator(observation_contract()).is_valid(payload)


@pytest.mark.parametrize('version', [1,2])
@pytest.mark.parametrize('change', ['reordered','foreign','duplicate','missing','extra','missing_value'])
def test_protocol_specific_schema_freezes_seed_population_without_freezing_order(version, change):
    from lib.research.observation_audit import observation_contract
    from jsonschema import Draft202012Validator
    payload = observation_payload(PROTOCOL, 'metrics')['scientific_observations']
    samples = payload['samples']
    if version == 2:
        payload['format'] = 'tofu.scientific-observations/v2'
        samples['method_seeds'] = list(samples['seeds'])
        samples['baseline_seeds'] = list(reversed(samples['seeds']))
    field = 'baseline_seeds' if version == 2 else 'seeds'
    if change == 'reordered': samples[field].reverse()
    elif change == 'foreign': samples[field][0] = 99
    elif change == 'duplicate': samples[field][0] = samples[field][1]
    elif change == 'missing': samples[field].pop()
    elif change == 'extra': samples[field].append(99)
    else: samples['method_values'].pop()
    validator = Draft202012Validator(observation_contract(PROTOCOL))
    assert validator.is_valid(payload) is (change == 'reordered')
    original = observation_contract()['properties']['samples']['properties']['seeds']
    assert 'enum' not in original['items'] and original['maxItems'] == 32


@pytest.mark.parametrize('seeds', [[], [1], [1,1], [True,2], list(range(33))])
def test_projected_seed_contract_cannot_widen_invalid_frozen_population(seeds):
    from lib.research.observation_audit import observation_contract
    with pytest.raises(ValueError):
        observation_contract({**PROTOCOL, 'random_seeds':seeds,
                              'decision_rule':{'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}})

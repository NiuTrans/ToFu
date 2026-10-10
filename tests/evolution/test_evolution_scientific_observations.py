"""Frozen generic stages recompute quantitative observations before review."""
from copy import deepcopy
import json
import pytest
from lib.evolution.contracts import EvolutionStateError,EvolutionContractError
from lib.evolution.tool_evidence import scientific_audit,scientific_audit_valid
from tests.evolution.test_evolution_runs import runtime as runtime,finish,OWNER
from tests.evolution.test_evolution_programs import runtime as _program_runtime_fixture
program_runtime = _program_runtime_fixture
from tests.evolution.test_evolution_background_execution import command as _background_command_fixture, complete
background_command = _background_command_fixture
from lib.tasks_pkg import background_commands as background_commands
from tests.research.test_research_decisions import result_workspace
from tests.support.research_observations import observation_payload
from lib.project_mod.run_command import _format_run_output

pytestmark=pytest.mark.unit


def protocol():
    value=result_workspace()['protocol']
    value['decision_rule']={'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}
    return value


def command(payload,*,call='measure',code=0):
    raw=_format_run_output('measure',json.dumps(payload),'',code)
    return {'toolRounds':[{'toolName':'run_command','toolCallId':call,'status':'done','toolArgs':json.dumps({'command':'measure'})}],
        'messages':[{'role':'tool','tool_call_id':call,'content':raw}]}


@pytest.mark.parametrize('change',['none','wrong_outcome','wrong_mean','missing_seed','read_only','unrelated_success','failed','missing','foreign_reply','bad_label'])
def test_frozen_protocol_only_accepts_correlated_consistent_measurements(runtime,change):
    runs,_,tasks=runtime;frozen=protocol()
    run=runs.submit_run({'objective':'Validate the frozen comparison','scientific_protocol':frozen,'max_attempts':1},
        user_id=OWNER,idempotency_key='scientific-'+change)
    assert run['scientific_protocol']==frozen and run['require_command_execution']
    payload=observation_payload(frozen,'metrics','supported')
    if change=='wrong_outcome':payload['scientific_outcome']='refuted'
    elif change=='wrong_mean':payload['scientific_observations']['samples']['mean_difference']=.99
    elif change=='missing_seed':payload['scientific_observations']['samples']['seeds']=[1]
    elif change=='bad_label':payload['scientific_outcome']=[]
    observed=command(payload,code=1 if change=='failed' else 0)
    if change=='read_only':observed['toolRounds'][0]['toolName']='read_files'
    elif change=='foreign_reply':observed['messages'][0]['tool_call_id']='foreign'
    elif change=='unrelated_success':
        observed['toolRounds'][0]['toolName']='read_files'
        auxiliary=command({'ok':True},call='auxiliary')
        for key in observed:observed[key]+=auxiliary[key]
    elif change=='missing':observed={'toolRounds':[],'messages':[]}
    tasks[0].update(observed)
    result=finish(runtime,tasks[0],'The experiment certainly proves the hypothesis.')
    from lib.evolution.stage_outcomes import read_metadata
    metadata=read_metadata(run['run_id'],tasks[0]['id'],user_id=OWNER)
    assert metadata['scientific_protocol']==frozen
    if change=='none':
        assert result['status']=='reviewing' and len(tasks)==2
        assert scientific_audit_valid(metadata['scientific_audit'])
        assert result['tool_evidence_source']['source_kind']=='tool_evidence'
        assert 'scientific_protocol' in tasks[1]['messages'][0]['content']
    else:
        assert result['status']=='failed' and len(tasks)==1
        assert not scientific_audit_valid(metadata['scientific_audit'])


@pytest.mark.parametrize('field,value',[('direction','lower'),('minimum_effect',.5),('uncertainty_multiplier',4)])
def test_idempotency_cannot_change_precommitted_decision(runtime,field,value):
    runs,_,tasks=runtime;frozen=protocol();payload={'objective':'Frozen comparison','scientific_protocol':frozen}
    run=runs.submit_run(payload,user_id=OWNER,idempotency_key='frozen-policy')
    changed=deepcopy(payload);changed['scientific_protocol']['decision_rule'][field]=value
    with pytest.raises(EvolutionStateError,match='Idempotency'):
        runs.submit_run(changed,user_id=OWNER,idempotency_key='frozen-policy')
    assert len(tasks)==1 and run['scientific_protocol']==frozen
    assert runs.submit_run(payload,user_id=OWNER,idempotency_key='frozen-policy')['run_id']==run['run_id']


@pytest.mark.parametrize('change',['missing_rule','one_seed','duplicate_seed','empty_metric','unknown_direction'])
def test_incomplete_protocol_does_not_allocate_executor(runtime,change):
    runs,_,tasks=runtime;value=protocol()
    if change=='missing_rule':value.pop('decision_rule')
    elif change=='one_seed':value['random_seeds']=[1]
    elif change=='duplicate_seed':value['random_seeds']=[1,1]
    elif change=='empty_metric':value['primary_metric']=' '
    else:value['decision_rule']['direction']='unknown'
    with pytest.raises(EvolutionContractError):runs.submit_run({'objective':'Compare','scientific_protocol':value},user_id=OWNER)
    assert not tasks


def test_counterevidence_cannot_be_overwritten_by_a_favorable_later_packet():
    value=protocol();first=command(observation_payload(value,'metrics','supported'),call='first')
    second=command(observation_payload(value,'metrics','refuted'),call='second')
    for key in first:first[key]+=second[key]
    before=deepcopy(first);audit=scientific_audit(first,value)
    assert audit['outcomes']=={'supported':1,'refuted':1,'inconclusive':0}
    assert not scientific_audit_valid(audit) and first==before


def test_long_review_delivery_reference_keeps_original_digest(runtime):
    import hashlib
    runs,_,tasks=runtime;value=protocol()
    run=runs.submit_run({'objective':'Compare','scientific_protocol':value},user_id=OWNER)
    original_prompt=tasks[0]['messages'][0]['content']
    tasks[0].update(command(observation_payload(value,'metrics','supported')))
    delivery='measured result '*3000
    result=finish(runtime,tasks[0],delivery)
    assert result['status']=='reviewing'
    assert result['result_source']['source_sha256']==hashlib.sha256(delivery.encode()).hexdigest()
    assert 'Scientific observation contract' in original_prompt


@pytest.mark.parametrize('claim',['supported','refuted'])
@pytest.mark.parametrize('foreground_auxiliary',[False,True])
def test_late_owned_measurement_is_audited_without_replay_or_rewriting_original(runtime,background_command,claim,foreground_auxiliary):
    from lib.evolution import stage_outcomes as outcomes
    runs,_,tasks=runtime;value=protocol()
    run=runs.submit_run({'objective':'Validate independent measurements','scientific_protocol':value,'max_attempts':1},user_id=OWNER)
    source,handle=background_command
    from tests.evolution.test_evolution_background_execution import attach_background_launch
    attach_background_launch(background_command,tasks[0])
    if foreground_auxiliary:
        auxiliary=command({'ok':True},call='setup')
        tasks[0]['toolRounds']+=auxiliary['toolRounds'];tasks[0]['messages']+=auxiliary['messages']
    waiting=finish(runtime,tasks[0],'Awaiting measured result, no conclusion yet.')
    assert waiting['background_wait'] and len(tasks)==1
    parent=deepcopy(outcomes.records.get_record(outcomes.NAMESPACE,outcomes._key(run['task_id'],OWNER)))
    payload=observation_payload(value,'metrics','supported');payload['scientific_outcome']=claim
    background_commands.finish_handed_off_command(command_id=handle,
        result=_format_run_output('experiment',json.dumps(payload),'',0),error=None)
    result=runs.advance_run(run['run_id'],user_id=OWNER)
    assert result['status']==('reviewing' if claim=='supported' else 'failed')
    assert len(tasks)==(2 if claim=='supported' else 1)
    assert outcomes.records.get_record(outcomes.NAMESPACE,outcomes._key(run['task_id'],OWNER))==parent
    joined=outcomes.read_metadata(run['run_id'],run['task_id'],user_id=OWNER)
    assert scientific_audit_valid(joined['background_scientific_audit'])==(claim=='supported')
    assert result['background_evidence_source']['source_kind']=='background_evidence'
    if claim=='supported':
        assert 'Do not execute tools.' not in tasks[1]['messages'][0]['content']
        page=outcomes.page(run['run_id'],run['task_id'],user_id=OWNER,source_kind='background_evidence')
        assert 'scientific_observations' in page['content']
        assert runs.advance_run(run['run_id'],user_id=OWNER)['status']=='reviewing' and len(tasks)==2


def test_valid_numeric_stage_completes_only_after_original_reaches_review_model(runtime):
    from tests.evolution.test_evolution_source_tools import delivered
    from tests.evolution.test_evolution_runs import review
    runs,_,tasks=runtime;value=protocol()
    run=runs.submit_run({'objective':'Validate measured improvement','scientific_protocol':value},user_id=OWNER)
    tasks[0].update(command(observation_payload(value,'metrics','supported')))
    reviewing=finish(runtime,tasks[0],'Paired improvement measured under the frozen rule.')
    reference=reviewing['tool_evidence_source'];reviewer=tasks[-1]
    delivered(reviewer,{'action':'read','run_id':run['run_id'],'task_id':reference['task_id'],
        'source_kind':'tool_evidence','source_sha256':reference['source_sha256'],'offset':0})
    final=finish(runtime,reviewer,review())
    assert final['status']=='completed' and len(tasks)==2


@pytest.mark.parametrize('observed,required,passed',[('supported','supported',True),('refuted','supported',False),('refuted','refuted',True)])
def test_valid_measurement_missing_numeric_target_cannot_be_overruled(runtime,observed,required,passed):
    runs,_,tasks=runtime;value=protocol()
    run=runs.submit_run({'objective':'Meet the frozen quantitative target','scientific_protocol':value,
        'scientific_required_outcome':required,'max_attempts':1},user_id=OWNER)
    tasks[0].update(command(observation_payload(value,'metrics',observed)))
    result=finish(runtime,tasks[0],'Trust me, all acceptance checks pass.')
    assert result['status']==('reviewing' if passed else 'failed')
    assert len(tasks)==(2 if passed else 1)
    if not passed:assert result['error']=='scientific_outcome_requirement_unmet'


def test_large_retained_packet_is_not_replaced_with_a_partial_excerpt():
    value=protocol();payload=observation_payload(value,'metrics','supported')
    payload['scientific_audit']['log']='diagnostic '*1000
    audit=scientific_audit(command(payload),value)
    assert scientific_audit_valid(audit)


def test_numeric_target_cannot_exist_without_frozen_protocol(runtime):
    runs,_,tasks=runtime
    with pytest.raises(EvolutionContractError):runs.submit_run({'objective':'Pass','scientific_required_outcome':'supported'},user_id=OWNER)
    assert not tasks


def test_custom_frontier_freezes_protocol_and_target_before_launch_intent(program_runtime):
    programs,_,launched=program_runtime;value=protocol()
    program=programs.create_program({'objective':'Meet the quantitative goal','steps':[
        {'kind':'experiment','title':'Measure','instruction':'Execute the precommitted comparison','checks':['Observed comparison'],
         'scientific_protocol':value,'scientific_required_outcome':'supported'},
        {'kind':'final','title':'Audit','instruction':'Audit the original objective','checks':['Original objective satisfied']}]},
        user_id=OWNER,idempotency_key='science-frontier')
    programs.command_program(program['program_id'],{'action':'start'},user_id=OWNER)
    assert len(launched)==1
    assert launched[0]['payload']['scientific_protocol']==value
    assert launched[0]['payload']['scientific_required_outcome']=='supported'
    assert launched[0]['payload']['require_command_execution']


def test_quantitative_policy_schema_is_loaded_only_when_requested(monkeypatch):
    from lib.evolution import contracts
    contracts.contract_validator.cache_clear()
    original=contracts.load_schema;loaded=[]
    def load(name):loaded.append(name);return original(name)
    monkeypatch.setattr(contracts,'load_schema',load)
    try:
        validator=contracts.contract_validator('evolution_run_v1.schema.json','request')
        validator.validate({'objective':'Ordinary task'})
        assert 'research_program_v1.schema.json' not in loaded
        validator.validate({'objective':'Quantitative task','scientific_protocol':protocol()})
        assert 'research_program_v1.schema.json' in loaded
    finally:contracts.contract_validator.cache_clear()


@pytest.mark.parametrize('substitute',[False,True])
def test_frozen_scope_controls_review_admission(runtime,substitute):
    runs,_,tasks=runtime;frozen=protocol()
    payload=observation_payload(frozen,'metrics','supported')
    comparison=payload['scientific_observations']['comparison']
    frozen['measurement_scope']={key:comparison['method'][key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    run=runs.submit_run({'objective':'Validate on the registered dataset and budget',
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    if substitute:
        for system in ('method','baseline'):comparison[system]['dataset_digest']='f'*64
    tasks[0].update(command(payload))
    result=finish(runtime,tasks[0],'Measured improvement.')
    assert result['scientific_protocol']==frozen
    assert result['status']==('failed' if substitute else 'reviewing')
    assert len(tasks)==(1 if substitute else 2)


@pytest.mark.parametrize('malformation',['truncated','log_prefix','failed_transport','unrelated'])
def test_background_scientific_attempt_cannot_disappear_behind_success(runtime,background_command,malformation):
    from lib.evolution import stage_outcomes as outcomes
    from tests.evolution.test_evolution_background_execution import attach_background_launch
    runs,_,tasks=runtime;value=protocol()
    run=runs.submit_run({'objective':'Reproduce every attempted measurement',
        'scientific_protocol':value,'max_attempts':1},user_id=OWNER)
    attach_background_launch(background_command,tasks[0])
    foreground=command(observation_payload(value,'metrics','supported'),call='foreground')
    for key in foreground:tasks[0][key]+=foreground[key]
    waiting=finish(runtime,tasks[0],'Await the remaining experiment.')
    assert waiting['background_wait'] and len(tasks)==1
    raw=json.dumps(observation_payload(value,'metrics','supported'))
    if malformation=='truncated':raw=raw[:raw.index('"scientific_observations"')+30]
    elif malformation=='log_prefix':raw='measurement log before JSON\n'+raw
    elif malformation=='unrelated':raw='setup completed'
    background_commands.finish_handed_off_command(command_id=background_command[1],
        result=_format_run_output('experiment',raw,'',1 if malformation=='failed_transport' else 0),error=None)
    settled=runs.advance_run(run['run_id'],user_id=OWNER)
    metadata=outcomes.read_metadata(run['run_id'],run['task_id'],user_id=OWNER)
    audit=metadata['background_scientific_audit']
    if malformation=='unrelated':
        assert settled['status']=='reviewing' and audit['failure_count']==0
    else:
        assert settled['status']=='failed'
        assert settled['error']==('background_command_execution_failed' if malformation=='failed_transport'
            else 'scientific_observations_unverified')
        assert audit['observation_count']==2 and audit['failure_count']==1
        assert audit['outcomes']['supported']==1 and len(tasks)==1
    page=outcomes.page(run['run_id'],run['task_id'],user_id=OWNER,source_kind='background_evidence')
    assert any(raw in json.loads(line)['content'] for line in page['content'].splitlines())


@pytest.mark.parametrize('mutation',['missing_count','missing_outcomes','missing_outcome',
    'boolean_count','negative_count','fractional_count','unclassified','excess_classified',
    'foreign_outcome','bad_digest','failed_attempt','legacy_policy'])
def test_incomplete_audit_cannot_authorize_review_or_negative_result_diagnosis(mutation):
    from lib.evolution.replanning import definitive_failure
    from lib.evolution.tool_evidence import scientific_outcome_met
    frozen=protocol()
    audit=scientific_audit(command(observation_payload(frozen,'metrics','refuted')),frozen)
    assert scientific_audit_valid(audit)
    if mutation=='missing_count':audit.pop('observation_count')
    elif mutation=='missing_outcomes':audit.pop('outcomes')
    elif mutation=='missing_outcome':audit['outcomes'].pop('supported')
    elif mutation=='boolean_count':audit['observation_count']=True
    elif mutation=='negative_count':audit['outcomes']['supported']=-1
    elif mutation=='fractional_count':audit['outcomes']['refuted']=1.5
    elif mutation=='unclassified':audit['observation_count']=2
    elif mutation=='excess_classified':audit['outcomes']['refuted']=2
    elif mutation=='foreign_outcome':audit['outcomes']['invented']=1
    elif mutation=='bad_digest':audit['evidence_sum']='not-a-digest'
    elif mutation=='failed_attempt':audit['failure_count']=1
    else:audit.pop('capture_policy_version')
    original=deepcopy(audit)
    assert not scientific_audit_valid(audit)
    assert not scientific_outcome_met(audit,'refuted')
    assert not definitive_failure({'status':'failed','error':'scientific_outcome_requirement_unmet',
        'scientific_protocol':frozen,'scientific_required_outcome':'supported'},scientific_audit=audit)
    assert audit==original


@pytest.mark.parametrize('outcome',['supported','refuted','inconclusive'])
def test_complete_repeated_audit_keeps_validity(outcome):
    frozen=protocol();payload=observation_payload(frozen,'metrics','inconclusive')
    samples=payload['scientific_observations']['samples']
    if outcome=='inconclusive':samples.update(method_values=list(samples['baseline_values']),mean_difference=0,standard_error=0)
    else:payload=observation_payload(frozen,'metrics',outcome)
    measured=command(payload,call='first');second=command(payload,call='second')
    for key in measured:measured[key]+=second[key]
    audit=scientific_audit(measured,frozen)
    assert audit['observation_count']==2 and scientific_audit_valid(audit)


@pytest.mark.parametrize('changed',['none','method','baseline','missing'])
def test_frozen_system_digest_controls_review_admission(runtime,changed):
    runs,_,tasks=runtime;frozen=protocol();payload=observation_payload(frozen,'metrics','supported')
    comparison=payload['scientific_observations']['comparison']
    frozen['measurement_scope']={key:comparison['method'][key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    frozen['measurement_scope'].update(candidate_digest='a'*64,baseline_digest='b'*64)
    active=runs.submit_run({'objective':'Validate the exact frozen candidate generation',
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    comparison['method']['system_digest']='a'*64;comparison['baseline']['system_digest']='b'*64
    if changed in ('method','baseline'):comparison[changed]['system_digest']='f'*64
    elif changed=='missing':comparison['baseline'].pop('system_digest')
    tasks[0].update(command(payload));settled=finish(runtime,tasks[0],'The measured result is reported.')
    assert settled['scientific_protocol']==frozen and settled['objective']==active['objective']
    assert settled['status']==('reviewing' if changed=='none' else 'failed')
    assert len(tasks)==(2 if changed=='none' else 1)
    if changed!='none':assert settled['error']=='scientific_observations_unverified'


@pytest.mark.parametrize('seeds',[[1.9,2.1],[1.0,2.0],['1','2'],[True,1,2],'123',list(range(33)),None])
def test_scientific_seed_plan_cannot_be_silently_coerced_or_truncated(runtime,seeds):
    runs,_,tasks=runtime;frozen=protocol();frozen['random_seeds']=seeds
    with pytest.raises(EvolutionContractError):
        runs.submit_run({'objective':'Preserve the exact precommitted experiment plan',
            'scientific_protocol':frozen},user_id=OWNER)
    assert not tasks


def test_evolution_executor_receives_version_bound_observation_contract():
    from lib.evolution.tool_evidence import scientific_execution_instruction
    frozen=protocol();payload=observation_payload(frozen,'metrics')
    context=payload['scientific_observations']['comparison']['method']
    frozen['measurement_scope']={key:context[key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    frozen['measurement_scope'].update(candidate_digest='a'*64,baseline_digest='b'*64)
    prompt=scientific_execution_instruction(frozen)
    contract=json.loads(prompt.split('Scientific observation contract: ',1)[1])
    for system in ('method','baseline'):
        assert 'system_digest' in contract['properties']['comparison']['properties'][system]['required']


@pytest.mark.parametrize('field',['system','budget_unit'])
def test_blank_report_context_cannot_reach_independent_review(runtime,field):
    runs,_,tasks=runtime;frozen=protocol();payload=observation_payload(frozen,'metrics','supported')
    comparison=payload['scientific_observations']['comparison'];comparison['method'][field]='  '
    if field=='budget_unit':comparison['baseline'][field]='  '
    runs.submit_run({'objective':'Compare named systems under declared resource units',
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    tasks[0].update(command(payload));settled=finish(runtime,tasks[0],'A supported effect was reported.')
    assert settled['status']=='failed' and settled['error']=='scientific_observations_unverified'
    assert len(tasks)==1


def test_identified_reordered_measurement_passes_native_scientific_audit():
    from lib.evolution.tool_evidence import scientific_audit, scientific_execution_instruction
    frozen = protocol()
    payload = observation_payload(frozen, 'metrics', 'supported')
    evidence = payload['scientific_observations']
    evidence['format'] = 'tofu.scientific-observations/v2'
    samples = evidence['samples']
    samples['method_seeds'] = list(reversed(samples['seeds']))
    samples['method_values'].reverse()
    samples['baseline_seeds'] = list(samples['seeds'])
    assert scientific_audit_valid(scientific_audit(command(payload), frozen))
    assert 'method_seeds' in scientific_execution_instruction(frozen)


def test_duplicate_scientific_outcome_cannot_select_favorable_last_value():
    from lib.evolution.tool_evidence import scientific_audit
    frozen = protocol()
    payload = observation_payload(frozen, 'metrics', 'supported')
    raw = json.dumps(payload)
    raw = '{"scientific_outcome":"refuted",' + raw[1:]
    observed = command(payload)
    observed['messages'][0]['content'] = _format_run_output('measure', raw, '', 0)
    audit = scientific_audit(observed, frozen)
    assert not scientific_audit_valid(audit)
    assert audit['failure_count'] == 1


@pytest.mark.parametrize('identified', [False, True])
def test_required_identified_format_is_frozen_and_enforced_before_review(runtime, identified):
    from lib.evolution.stage_outcomes import read_metadata
    runs, _, tasks = runtime
    frozen = protocol(); frozen['observation_format'] = 'tofu.scientific-observations/v2'
    request = {'objective':'Validate identified paired measurements', 'scientific_protocol':frozen, 'max_attempts':1}
    run = runs.submit_run(request, user_id=OWNER, idempotency_key='identified-required')
    payload = observation_payload({key:value for key,value in frozen.items() if key != 'observation_format'}, 'metrics', 'supported')
    if identified:
        evidence = payload['scientific_observations']; evidence['format'] = frozen['observation_format']
        evidence['samples']['method_seeds'] = list(evidence['samples']['seeds'])
        evidence['samples']['baseline_seeds'] = list(evidence['samples']['seeds'])
    tasks[0].update(command(payload))
    result = finish(runtime, tasks[0], 'The experiment supports the candidate.')
    assert result['status'] == ('reviewing' if identified else 'failed')
    metadata = read_metadata(run['run_id'], tasks[0]['id'], user_id=OWNER)
    assert metadata['scientific_protocol']['observation_format'] == frozen['observation_format']
    assert scientific_audit_valid(metadata['scientific_audit']) is identified
    changed = deepcopy(request); changed['scientific_protocol'].pop('observation_format')
    with pytest.raises(EvolutionStateError, match='Idempotency'):
        runs.submit_run(changed, user_id=OWNER, idempotency_key='identified-required')

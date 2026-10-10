"""An executor precommits future validation before any validation launch."""
from copy import deepcopy
import pytest
from tests.evolution.test_evolution_programs import runtime as runtime,create,launch,OWNER
from tests.evolution.test_evolution_exploration_tools import bound_task,call
from tests.evolution.test_evolution_scientific_observations import protocol
from lib.evolution.contracts import EvolutionStateError,EvolutionContractError

pytestmark=pytest.mark.unit


def setup(runtime, *, scope_required=False):
    programs=runtime[0]
    program=programs.create_program({'objective':'Validate the original claim','steps':[
        {'kind':'synthesis','title':'Freeze','instruction':'Freeze criterion before validation','checks':['Preregistration exists']},
        {'kind':'experiment','title':'Validate','instruction':'Perform held-out validation','checks':['Measured effect'],'requires_scientific_protocol':True,
         **({'requires_frozen_measurement_scope':True} if scope_required else {})},
        {'kind':'final','title':'Audit','instruction':'Audit original objective','checks':['Original goal satisfied']}]},
        user_id=OWNER,idempotency_key='preregister-test')
    program=launch(runtime,program);task=bound_task(runtime,program)
    target=call(task,program,'validation_context')['targets'][0]
    return program,task,target


def test_preregistration_is_immutable_idempotent_and_launches_nothing(runtime):
    programs,store,launched=runtime;program,task,target=setup(runtime)
    value=protocol();args={'target_step_sha256':target['target_step_sha256'],'scientific_protocol':value,'scientific_required_outcome':'supported'}
    first=call(task,program,'preregister',**args);writes=store.writes
    assert first==call(task,program,'preregister',**args) and store.writes==writes
    updated=programs.get_program(program['program_id'],user_id=OWNER)
    assert updated['queue'][1]['scientific_protocol']==value
    assert updated['current']==program['current'] and updated['objective']==program['objective']
    assert updated['acceptance']==program['acceptance'] and len(launched)==1
    assert 'not measurement' in first['evidence_status']
    changed=deepcopy(args);changed['scientific_protocol']['decision_rule']['minimum_effect']=-1
    with pytest.raises(EvolutionStateError,match='already frozen'):call(task,program,'preregister',**changed)


@pytest.mark.parametrize('change',['current','foreign','paused','reviewer','missing_rule'])
def test_wrong_authority_or_plan_cannot_change_future_validation(runtime,change):
    programs,store,launched=runtime;program,task,target=setup(runtime)
    args={'target_step_sha256':target['target_step_sha256'],'scientific_protocol':protocol(),'scientific_required_outcome':'supported'}
    if change=='current':args['target_step_sha256']=programs.validation_step_digest(program['current']['step'])
    elif change=='foreign':task['_userId']=OWNER+1
    elif change=='paused':programs.command_program(program['program_id'],{'action':'pause'},user_id=OWNER)
    elif change=='reviewer':task['config']['evolution']['stage']='reviewing'
    else:args['scientific_protocol'].pop('decision_rule')
    with pytest.raises((EvolutionStateError,EvolutionContractError)):call(task,program,'preregister',**args)
    assert not programs.get_program(program['program_id'],user_id=OWNER)['queue'][1].get('scientific_protocol')
    assert len(launched)==1


def test_tool_schema_supplies_complete_numeric_contract():
    from lib.evolution.exploration_tools import tool_definition
    parameters=tool_definition()['function']['parameters'];spec=parameters['properties']['scientific_protocol']
    assert 'decision_rule' in spec['required']
    assert spec['properties']['random_seeds']['minItems']==2
    assert 'preregister' in parameters['properties']['action']['enum']

from tests.evolution.test_evolution_runs import runtime as _task_runtime_fixture, finish as finish_task
task_runtime = _task_runtime_fixture


@pytest.mark.parametrize('registered',[False,True])
def test_synthesis_cannot_claim_registration_without_durable_future_policy(task_runtime,registered):
    from lib.evolution import programs
    from lib.evolution.exploration_tools import execute
    from tests.evolution.test_evolution_runs import OWNER as task_owner
    from lib.evolution.stage_outcomes import read_metadata
    runs,_,tasks=task_runtime
    program=programs.create_program({'objective':'Validate a quantitative result','max_attempts':1,'steps':[
        {'kind':'synthesis','title':'Freeze','instruction':'Precommit','checks':['Preregistration exists']},
        {'kind':'experiment','title':'Validate','instruction':'Reproduce','checks':['Measured result'],'requires_scientific_protocol':True},
        {'kind':'final','title':'Audit','instruction':'Audit','checks':['Original goal satisfied']}]},
        user_id=task_owner,idempotency_key='real-prereg-'+str(registered))
    active=programs.command_program(program['program_id'],{'action':'start'},user_id=task_owner)
    executor=tasks[0];run=runs.get_run(active['current']['run_id'],user_id=task_owner)
    target=run['validation_preregistration_target']
    if registered:
        execute(executor,{'program_id':program['program_id'],'action':'preregister','target_step_sha256':target['target_step_sha256'],
            'scientific_protocol':protocol(),'scientific_required_outcome':'supported'},call_id='real-freeze')
    result=finish_task(task_runtime,executor,'I have preregistered the entire validation protocol.')
    metadata=read_metadata(run['run_id'],executor['id'],user_id=task_owner)
    assert metadata['validation_preregistration_observed']==registered
    assert len(tasks)==(2 if registered else 1)
    assert result['status']==('reviewing' if registered else 'failed')
    if not registered:
        assert result['error']=='validation_preregistration_missing'
        updated=programs.advance_program(program['program_id'],user_id=task_owner)
        assert updated['queue'][0]['kind']=='replan'
        assert updated['queue'][1]['kind']=='synthesis'
        assert updated['objective']==program['objective'] and updated['acceptance']==program['acceptance']


def test_default_frontier_precommits_before_independent_validation(runtime):
    programs=runtime[0]
    program=programs.create_program({'objective':'Improve the system'},user_id=OWNER,idempotency_key='default-prereg')
    synthesis=next(step for step in program['queue'] if step['kind']=='synthesis')
    validation=program['queue'][-2]
    assert 'action=preregister' in synthesis['instruction']
    assert validation['requires_scientific_protocol'] and validation['require_command_execution']
    assert validation['requires_frozen_measurement_scope'] is True
    assert validation['required_observation_format'] == 'tofu.scientific-observations/v2'
    assert not validation.get('scientific_protocol')


def test_reviewed_registration_launches_validation_with_exact_frozen_policy(task_runtime):
    import json
    from lib.evolution import programs
    from lib.evolution.exploration_tools import execute
    from tests.evolution.test_evolution_runs import OWNER as task_owner,review
    runs,_,tasks=task_runtime;value=protocol()
    program=programs.create_program({'objective':'Validate improvement','max_attempts':1,'steps':[
        {'kind':'synthesis','title':'Freeze','instruction':'Precommit','checks':['Preregistration exists']},
        {'kind':'experiment','title':'Validate','instruction':'Measure','checks':['Measured result'],'requires_scientific_protocol':True},
        {'kind':'final','title':'Audit','instruction':'Audit','checks':['Original goal satisfied']}]},
        user_id=task_owner,idempotency_key='complete-prereg-flow')
    active=programs.command_program(program['program_id'],{'action':'start'},user_id=task_owner)
    first=runs.get_run(active['current']['run_id'],user_id=task_owner);executor=tasks[0]
    execute(executor,{'program_id':program['program_id'],'action':'preregister',
        'target_step_sha256':first['validation_preregistration_target']['target_step_sha256'],
        'scientific_protocol':value,'scientific_required_outcome':'supported'},call_id='freeze-before-validation')
    finish_task(task_runtime,executor,'The next validation protocol was frozen.')
    judgment=json.loads(review())
    judgment['requirement_results']=[{'requirement_id':'requirement-1','outcome':'pass','evidence_ref':'Program CAS contains the frozen future policy.'}]
    finish_task(task_runtime,tasks[-1],json.dumps(judgment))
    for _ in range(3):active=programs.advance_program(program['program_id'],user_id=task_owner)
    second=runs.get_run(active['current']['run_id'],user_id=task_owner)
    assert second['run_id']!=first['run_id']
    assert second['scientific_protocol']==value and second['scientific_required_outcome']=='supported'
    assert second['require_command_execution']
    assert len(tasks)==3


def test_registered_measurement_scope_is_preserved_and_immutable(runtime):
    from tests.support.research_observations import observation_payload
    programs,store,launched=runtime;program,task,target=setup(runtime)
    value=protocol();context=observation_payload(value,'metrics')['scientific_observations']['comparison']['method']
    value['measurement_scope']={key:context[key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    args={'target_step_sha256':target['target_step_sha256'],'scientific_protocol':value,'scientific_required_outcome':'supported'}
    call(task,program,'preregister',**args)
    assert programs.get_program(program['program_id'],user_id=OWNER)['queue'][1]['scientific_protocol']==value
    changed=deepcopy(args);changed['scientific_protocol']['measurement_scope']['budget']+=1
    with pytest.raises(EvolutionStateError):call(task,program,'preregister',**changed)
    assert len(launched)==1


@pytest.mark.parametrize('scope_kind', ['missing', 'inputs_only', 'complete'])
def test_new_scope_policy_requires_inputs_budget_and_both_generations(runtime, scope_kind):
    from tests.support.research_observations import observation_payload
    programs, store, launched = runtime
    program, task, target = setup(runtime, scope_required=True)
    assert target['requires_frozen_measurement_scope'] is True
    value = protocol()
    if scope_kind != 'missing':
        context = observation_payload(value, 'metrics')['scientific_observations']['comparison']['method']
        value['measurement_scope'] = {key: context[key] for key in ('dataset_digest', 'split_digest', 'budget_unit', 'budget')}
        if scope_kind == 'complete':
            value['measurement_scope'].update(candidate_digest='c'*64, baseline_digest='b'*64)
    args = {'target_step_sha256': target['target_step_sha256'],
            'scientific_protocol': value, 'scientific_required_outcome': 'supported'}
    writes = store.writes
    if scope_kind != 'complete':
        with pytest.raises(EvolutionStateError, match='frozen'):
            call(task, program, 'preregister', **args)
        assert store.writes == writes
        assert programs.get_program(program['program_id'], user_id=OWNER) == program
    else:
        registered = call(task, program, 'preregister', **args)
        assert registered['scientific_protocol'] == value
        assert programs.validation_preregistered(
            {'program_id': program['program_id'], 'target_step_sha256': target['target_step_sha256']},
            program['current']['run_id'], user_id=OWNER)
        changed = deepcopy(args)
        changed['scientific_protocol']['measurement_scope']['candidate_digest'] = 'd'*64
        with pytest.raises(EvolutionStateError, match='already frozen'):
            call(task, program, 'preregister', **changed)
    assert len(launched) == 1


def test_scope_policy_cannot_be_bypassed_by_prefilled_weak_protocol(runtime):
    programs = runtime[0]
    with pytest.raises(EvolutionStateError, match='frozen'):
        programs.create_program({'objective': 'Validate fixed generations', 'steps': [
            {'kind': 'experiment', 'title': 'Validate', 'instruction': 'Measure',
             'checks': ['Measured result'], 'requires_scientific_protocol': True,
             'requires_frozen_measurement_scope': True, 'scientific_protocol': protocol(),
             'scientific_required_outcome': 'supported'},
            {'kind': 'final', 'title': 'Audit', 'instruction': 'Audit', 'checks': ['Original goal satisfied']}]},
            user_id=OWNER, idempotency_key='weak-prefilled-scope')


@pytest.mark.parametrize('change', ['missing_recovery', 'foreign_recovery', 'missing_protocol_policy', 'disabled_protocol_policy'])
def test_scope_extension_preserves_existing_step_constraints(change):
    from lib.evolution.contracts import validate_program
    step = {'kind': 'experiment', 'title': 'Validate', 'instruction': 'Measure', 'checks': ['Measured result']}
    if change == 'missing_recovery':
        step['kind'] = 'replan'
    elif change == 'foreign_recovery':
        step['recovery_of'] = 1
    else:
        step['requires_frozen_measurement_scope'] = True
        if change == 'disabled_protocol_policy':
            step['requires_scientific_protocol'] = False
    with pytest.raises(EvolutionContractError):
        validate_program(step, 'step')


def test_unfrozen_strict_validation_never_dispatches_executor(runtime):
    programs, store, launched = runtime
    step = {'kind': 'experiment', 'title': 'Validate', 'instruction': 'Measure',
            'checks': ['Measured result'], 'requires_scientific_protocol': True,
            'requires_frozen_measurement_scope': True}
    program = programs.create_program({'objective': 'Validate fixed generations', 'steps': [
        step, {'kind': 'final', 'title': 'Audit', 'instruction': 'Audit', 'checks': ['Original goal satisfied']}]},
        user_id=OWNER, idempotency_key='unfrozen-strict-launch')
    result = programs.command_program(program['program_id'], {'action': 'start'}, user_id=OWNER)
    assert result['status'] == 'needs_attention'
    assert 'frozen' in result['error']
    assert result['current'] is None and result['settled_through'] == 0
    assert result['queue'][0] == step and result['objective'] == program['objective']
    assert launched == []



def test_strict_validation_context_exposes_complete_required_fields_without_writes(runtime):
    programs, store, launched = runtime
    program, task, target = setup(runtime, scope_required=True)
    before = store.writes
    again = call(task, program, 'validation_context')['targets'][0]
    assert set(again['required_measurement_scope_fields']) == {
        'dataset_digest', 'split_digest', 'budget_unit', 'budget', 'candidate_digest', 'baseline_digest'}
    assert len(again['required_measurement_scope_fields']) == 6
    assert again == target and store.writes == before and len(launched) == 1
    with pytest.raises(EvolutionStateError) as failure:
        call(task, program, 'preregister', target_step_sha256=target['target_step_sha256'],
             scientific_protocol=protocol(), scientific_required_outcome='supported')
    for name in target['required_measurement_scope_fields']:
        assert name in str(failure.value)
    assert store.writes == before


def test_existing_validation_context_does_not_invent_strict_policy(runtime):
    program, task, target = setup(runtime)
    assert 'requires_frozen_measurement_scope' not in target
    assert 'required_measurement_scope_fields' not in target


def test_new_default_validation_preregistration_freezes_identified_format(runtime):
    programs = runtime[0]
    from tests.evolution.test_evolution_programs import launch, finish
    from tests.support.research_observations import synthetic_frozen_scope
    program = programs.create_program({'objective':'Measure an improvement.'}, user_id=OWNER, idempotency_key='identified-default')
    program = launch(runtime, program)
    for _ in range(3):
        program = launch(runtime, finish(runtime, program))
    target = programs.pending_validations(program)[0]
    value = protocol(); value['measurement_scope'] = synthetic_frozen_scope(value)
    result = programs.preregister_validation(program['program_id'], target['target_step_sha256'], value, 'supported',
        user_id=OWNER, executor_run_id=program['current']['run_id'])
    assert result['scientific_protocol']['observation_format'] == target['required_observation_format']
    assert 'observation_format' not in value
    saved, version = programs._read(program['program_id'], OWNER)
    validation = next(step for step in saved['queue'][1:] if step.get('requires_scientific_protocol'))
    validation['scientific_protocol'].pop('observation_format')
    programs._save(saved, version, OWNER)
    binding = {'program_id':program['program_id'], 'target_step_sha256':target['target_step_sha256']}
    assert not programs.validation_preregistered(binding, program['current']['run_id'], user_id=OWNER)
    ready = finish(runtime, saved)
    before = len(runtime[2])
    refused = programs.advance_program(ready['program_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and refused['current'] is None
    assert 'observation format' in refused['error']
    assert len(runtime[2]) == before

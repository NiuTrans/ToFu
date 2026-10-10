"""Evidence-gated diagnosis, atomic plan admission and original requirement retention."""
from copy import deepcopy
import json

import pytest

from lib.storage.errors import StorageError
from tests.evolution.test_evolution_programs import runtime as runtime, create, launch, command, finish, step, OWNER
from tests.evolution.test_evolution_exploration_tools import bound_task, call, candidate, queued_candidate

pytestmark = pytest.mark.unit


def fail_review(runtime, program, **overrides):
    programs = runtime[0]
    rid = program['current']['run_id']
    run = programs.runs.get_run(rid, user_id=OWNER)
    run.update(status='needs_attention', result='Observed baseline regression under concurrent handoffs.',
               review={'outcome': 'failure', 'criterion_results': [
                   {'criterion_id': 'objective', 'outcome': 'fail', 'evidence_ref': 'Observed failing trace.'}]},
               actuals={'tokens': 23, 'wall_clock_seconds': 4, 'tool_calls': 3}, error='')
    run.update(overrides)
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(rid, OWNER, kind='run'), run, kind='run')
    return command(runtime, program, 'advance')


def diagnosis(runtime, program):
    command(runtime, program, 'advance')
    return command(runtime, program, 'advance')


def propose(runtime, program):
    task = bound_task(runtime, program)
    result = call(task, program, 'replan', reason='The inspected trace disproves the serialized handoff assumption.',
                  steps=[candidate()])
    return task, result


def test_failure_is_archived_and_ordinary_diagnosis_replaces_no_original_checks(runtime):
    initial = launch(runtime, create(runtime))
    failed = fail_review(runtime, initial)
    assert failed['status'] == 'active' and failed['settled_through'] == 1
    assert failed['queue'][0]['kind'] == 'replan' and failed['queue'][0]['recovery_of'] == 1
    assert failed['queue'][1:] == initial['queue'] and len(runtime[2]) == 1
    current = diagnosis(runtime, failed)
    seed = json.loads(current['current']['context_seed'])
    assert seed['failure_diagnosis']['failed_sequence'] == 1
    assert seed['recent_experiments'][0]['outcome'] == 'needs_attention'
    task, proposed = propose(runtime, current)
    assert 'unverified plan' in proposed['evidence_status']
    staged = runtime[0].get_program(initial['program_id'], user_id=OWNER)
    assert staged['queue'] == failed['queue']  # review has not admitted candidates
    assert len(runtime[2]) == 2
    reviewed = finish(runtime, current)
    assert reviewed['status'] == 'active'
    assert reviewed['queue'] == [queued_candidate()] + initial['queue']
    assert reviewed['objective'] == initial['objective'] and reviewed['acceptance'] == initial['acceptance']
    assert reviewed['actuals']['tokens'] == 46
    assert 'replan_proposal' not in reviewed
    history = runtime[0].experiment_page(initial['program_id'], user_id=OWNER)['experiments']
    assert history[0]['result'].startswith('Observed baseline regression')
    assert history[1]['replan']['source_run_id'] == current['current']['run_id']
    next_run = diagnosis(runtime, reviewed)
    assert next_run['current']['step'] == queued_candidate()
    task = bound_task(runtime, next_run)
    memories = call(task, next_run, 'recall', query='serialized handoff assumption')
    assert [receipt['sequence'] for receipt in memories['experiments']] == [2]


@pytest.mark.parametrize('case', ['unknown', 'missing_review', 'missing_task', 'budget', 'manual', 'legacy'])
def test_uncertain_failures_limits_and_manual_policy_never_auto_replan(runtime, case):
    initial = launch(runtime, create(runtime, auto_replan=case != 'manual'))
    if case == 'legacy':
        value, version = runtime[0]._read(initial['program_id'], OWNER)
        value.pop('auto_replan');runtime[0]._save(value, version, OWNER)
    overrides = {}
    if case == 'unknown':
        overrides['review'] = {'outcome': 'failure', 'criterion_results': [{'outcome': 'unknown'}]}
    elif case == 'missing_review': overrides['review'] = None
    elif case in ('missing_task', 'budget'):
        overrides['error'] = 'task_unavailable; execution was not replayed' if case == 'missing_task' else 'execution_budget_exhausted'
    failed = fail_review(runtime, initial, **overrides)
    assert failed['status'] == 'needs_attention' and failed['queue'] == initial['queue']
    command(runtime, failed, 'advance')
    assert len(runtime[2]) == 1


def test_paused_failure_and_review_store_followups_but_dispatch_nothing(runtime):
    initial = launch(runtime, create(runtime))
    command(runtime, initial, 'pause')
    failed = fail_review(runtime, initial)
    assert failed['status'] == 'paused' and failed['queue'][0]['kind'] == 'replan'
    command(runtime, failed, 'advance'); assert len(runtime[2]) == 1
    command(runtime, failed, 'resume'); current = diagnosis(runtime, failed)
    task, _ = propose(runtime, current)
    command(runtime, current, 'pause')
    with pytest.raises(runtime[0].EvolutionStateError, match='paused'):
        call(task, current, 'replan', reason='Same reason', steps=[candidate()])
    reviewed = finish(runtime, current)
    assert reviewed['status'] == 'paused' and reviewed['queue'][0] == queued_candidate()
    command(runtime, reviewed, 'advance'); assert len(runtime[2]) == 2


def test_plan_is_frozen_and_ambiguous_commit_retries_once(runtime, monkeypatch):
    current = diagnosis(runtime, fail_review(runtime, launch(runtime, create(runtime))))
    task = bound_task(runtime, current)
    original = runtime[0]._save
    def lost_ack(value, version, user_id):
        saved = original(value, version, user_id)
        if value.get('replan_proposal'): raise StorageError('database_timeout', 'committed acknowledgement lost')
        return saved
    monkeypatch.setattr(runtime[0], '_save', lost_ack)
    args = dict(reason='Inspected baseline failure requires a new mechanism.', steps=[candidate()])
    with pytest.raises(StorageError): call(task, current, 'replan', **args)
    monkeypatch.setattr(runtime[0], '_save', original)
    first = call(task, current, 'replan', **args)
    assert first == call(task, current, 'replan', **args)
    with pytest.raises(runtime[0].EvolutionStateError, match='frozen'):
        call(task, current, 'replan', reason='Different cause', steps=[candidate()])
    settled = finish(runtime, current)
    assert settled['queue'].count(queued_candidate()) == 1


def test_settlement_ack_loss_does_not_duplicate_followups_or_cost(runtime, monkeypatch):
    initial = launch(runtime, create(runtime))
    current = diagnosis(runtime, fail_review(runtime, initial)); propose(runtime, current)
    original = runtime[0]._save
    def unavailable(value, version, user_id):
        if value['settled_through'] == 2: raise StorageError('database_unavailable', 'header not committed')
        return original(value, version, user_id)
    monkeypatch.setattr(runtime[0], '_save', unavailable)
    with pytest.raises(StorageError): finish(runtime, current)
    monkeypatch.setattr(runtime[0], '_save', original)
    settled = command(runtime, current, 'advance')
    assert settled['queue'] == [queued_candidate()] + initial['queue']
    assert settled['actuals']['tokens'] == 46 and settled['settled_through'] == 2
    command(runtime, settled, 'advance')
    assert runtime[0].get_program(initial['program_id'], user_id=OWNER)['actuals']['tokens'] == 46


@pytest.mark.parametrize('passed', [False, True])
def test_failed_diagnosis_or_missing_plan_does_not_admit_followups(runtime, passed):
    initial = launch(runtime, create(runtime))
    current = diagnosis(runtime, fail_review(runtime, initial))
    if not passed: propose(runtime, current)
    settled = finish(runtime, current, 'completed' if passed else 'needs_attention')
    assert settled['status'] == 'needs_attention' and settled['queue'][1:] == initial['queue']
    assert len([s for s in settled['queue'] if s['kind'] == 'replan']) == 1
    assert 'replan_proposal' not in settled


def test_final_audit_failure_inserts_diagnosis_but_keeps_original_final_required(runtime):
    initial = launch(runtime, create(runtime, steps=[step('final')]))
    current = diagnosis(runtime, fail_review(runtime, initial)); propose(runtime, current)
    settled = finish(runtime, current)
    assert settled['status'] == 'active' and settled['queue'][-1] == initial['queue'][0]
    assert len([s for s in settled['queue'] if s['kind'] == 'final']) == 1


def test_cancellation_of_diagnosis_fences_proposals_and_successors(runtime):
    current = diagnosis(runtime, fail_review(runtime, launch(runtime, create(runtime))))
    task, _ = propose(runtime, current)
    cancelled = command(runtime, current, 'cancel')
    with pytest.raises(runtime[0].EvolutionStateError):
        call(task, current, 'replan', reason='Again', steps=[candidate()])
    command(runtime, cancelled, 'advance')
    assert len(runtime[2]) == 2 and cancelled['status'] == 'cancelled'


def test_manual_and_explicit_unlimited_attempts_keep_original_authorization(runtime):
    manual = command(runtime, create(runtime, auto_replan=False), 'start')
    assert 'max_attempts' not in manual['current']['payload']
    unlimited = command(runtime, create(runtime, 'unlimited', max_attempts=0), 'start')
    assert unlimited['current']['payload']['max_attempts'] == 0


def test_replan_requires_diagnosis_and_frontier_has_a_durable_bound(runtime):
    initial = launch(runtime, create(runtime))
    task = bound_task(runtime, initial)
    with pytest.raises(runtime[0].EvolutionStateError, match='diagnosis'):
        call(task, initial, 'replan', reason='A speculative failure', steps=[candidate()])
    full = launch(runtime, create(runtime, 'full', steps=[step(title=str(n)) for n in range(31)] + [step('final')]))
    failed = fail_review(runtime, full)
    assert failed['status'] == 'needs_attention' and 'frontier_full' in failed['error']
    assert len(failed['queue']) == 32


# Use the actual task admission, run transitions, review parser and episode store.
from tests.evolution.test_evolution_runs import runtime as _task_runtime_fixture, finish as finish_task
task_runtime = _task_runtime_fixture


def test_three_reviewed_failures_reach_ordinary_diagnosis_and_review_gate(task_runtime):
    from lib.evolution import programs, get_episode
    from lib.evolution.exploration_tools import execute
    runs, _, tasks = task_runtime
    owner = 123
    initial = programs.create_program({'objective': 'Build a usable research handoff.',
        'steps': [step(), step('final')]}, user_id=owner, idempotency_key='actual-replan')
    pid = initial['program_id']
    active = programs.command_program(pid, {'action': 'start'}, user_id=owner)
    assert active['current']['payload']['max_attempts'] == 3
    def review_for(run, outcome):
        return json.dumps({'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [
            {'criterion_id': key, 'outcome': outcome, 'evidence_ref': 'Observed command result.'}
            for key in runs.RUBRIC], 'requirement_results': [
            {'requirement_id': f'requirement-{i+1}', 'outcome': outcome, 'evidence_ref': 'Observed requirement evidence.'}
            for i in range(len(run['acceptance_requirements']))]})
    for _ in range(3):
        execution = tasks[-1]
        run = finish_task(task_runtime, execution, 'The baseline reproduces a handoff failure.')
        reviewed = finish_task(task_runtime, tasks[-1], review_for(run, 'fail'))
    assert reviewed['status'] == 'needs_attention' and reviewed['attempt'] == 3
    assert get_episode(reviewed['episode_id'], user_id=owner)['status'] == 'failure'
    failed = programs.advance_program(pid, user_id=owner)
    assert failed['queue'][0]['kind'] == 'replan' and len(tasks) == 6
    programs.advance_program(pid, user_id=owner)
    diagnosing = programs.advance_program(pid, user_id=owner)
    assert diagnosing['current']['step']['kind'] == 'replan' and len(tasks) == 7
    executor = tasks[-1]
    response = execute(executor, {'program_id': pid, 'action': 'replan',
        'reason': 'Observed concurrent handoff invalidates the serial protocol.', 'steps': [candidate()]}, call_id='actual-plan')
    assert response['failed_sequence'] == 1
    run = finish_task(task_runtime, executor, 'Inspected experiment 1 and submitted competing falsifiable follow-ups.')
    finish_task(task_runtime, tasks[-1], review_for(run, 'pass'))
    settled = programs.advance_program(pid, user_id=owner)
    assert settled['status'] == 'active' and settled['queue'] == [queued_candidate()] + initial['queue']
    assert settled['actuals']['tokens'] == 120
    assert len(programs.experiment_page(pid, user_id=owner)['experiments']) == 2


def test_replanning_cannot_submit_the_same_failed_mechanism_under_a_new_title(runtime):
    initial = launch(runtime, create(runtime, steps=[queued_candidate(), step('final')]))
    current = diagnosis(runtime, fail_review(runtime, initial))
    task = bound_task(runtime, current)
    repeated = candidate(); repeated['title'] = 'Renamed failed approach'
    with pytest.raises(runtime[0].EvolutionStateError, match='failed technique'):
        call(task, current, 'replan', reason='Repeat it unchanged.', steps=[repeated])
    alternative = candidate(); alternative['hypothesis']['mechanism'] = 'A causal boundary with durable evidence references.'
    call(task, current, 'replan', reason='Change the mechanism exposed by the failure.', steps=[alternative])
    assert runtime[0].get_program(initial['program_id'], user_id=OWNER)['replan_proposal']['steps'][0]['hypothesis']['mechanism'] == alternative['hypothesis']['mechanism']


def test_pause_racing_proposal_cas_refuses_admission_without_losing_control(runtime, monkeypatch):
    current = diagnosis(runtime, fail_review(runtime, launch(runtime, create(runtime))))
    task = bound_task(runtime, current)
    original = runtime[0]._save
    def racing(value, version, user_id):
        if value.get('replan_proposal'):
            monkeypatch.setattr(runtime[0], '_save', original)
            command(runtime, current, 'pause')
        return original(value, version, user_id)
    monkeypatch.setattr(runtime[0], '_save', racing)
    with pytest.raises(StorageError) as conflict:
        call(task, current, 'replan', reason='Observed evidence suggests an alternative.', steps=[candidate()])
    assert conflict.value.code == 'database_conflict'
    latest = runtime[0].get_program(current['program_id'], user_id=OWNER)
    assert latest['status'] == 'paused' and 'replan_proposal' not in latest
    assert len(runtime[2]) == 2


@pytest.mark.parametrize('case',['negative','inconclusive','invalid','manual','missing_original','foreign_protocol'])
def test_scientific_negative_result_replans_only_from_frozen_valid_original(task_runtime,case):
    from lib.evolution import programs,stage_outcomes
    from tests.evolution.test_evolution_scientific_observations import protocol,command as measured_command
    from tests.support.research_observations import observation_payload
    from tests.evolution.test_evolution_runs import OWNER as owner
    runs,store,tasks=task_runtime;frozen=protocol()
    initial=programs.create_program({'objective':'Find a robust measured improvement',
        'auto_replan':case!='manual','steps':[
            {**step(),'scientific_protocol':frozen,'scientific_required_outcome':'supported'},step('final')]},
        user_id=owner,idempotency_key='scientific-replan-'+case)
    pid=initial['program_id'];active=programs.command_program(pid,{'action':'start'},user_id=owner)
    executor=tasks[0]
    payload=observation_payload(frozen,'metrics','inconclusive' if case=='inconclusive' else 'refuted')
    if case=='inconclusive':
        samples=payload['scientific_observations']['samples']
        samples.update(method_values=list(samples['baseline_values']),mean_difference=0,standard_error=0)
    if case=='invalid':payload['scientific_observations']['samples']['mean_difference']=999
    executor.update(measured_command(payload))
    failed=finish_task(task_runtime,executor,'The measurement did not support the hypothesis.')
    assert failed['status']=='failed' and len(tasks)==1
    if case in ('missing_original','foreign_protocol'):
        key=(stage_outcomes.NAMESPACE,stage_outcomes._key(executor['id'],owner))
        if case=='missing_original':store.rows.pop(key)
        else:store.rows[key]['scientific_protocol']['primary_metric']='foreign-metric'
    if case=='missing_original':
        with pytest.raises(programs.EvolutionStateError,match='unavailable'):
            programs.advance_program(pid,user_id=owner)
        retained=programs.get_program(pid,user_id=owner)
        assert retained['current']==active['current'] and retained['queue']==initial['queue']
        assert len(tasks)==1
        return
    settled=programs.advance_program(pid,user_id=owner)
    eligible=case in ('negative','inconclusive')
    assert settled['status']==('active' if eligible else 'needs_attention')
    assert settled['queue']==([programs.replanning.diagnosis_step(1)]+initial['queue'] if eligible else initial['queue'])
    assert settled['objective']==initial['objective'] and settled['acceptance']==initial['acceptance']
    receipt=programs.experiment_page(pid,user_id=owner)['experiments'][0]
    assert receipt['error']==failed['error'] and receipt['outcome']=='failed'
    assert len(tasks)==1  # Settlement records the frontier without dispatch.
    if eligible:
        programs.advance_program(pid,user_id=owner)
        diagnosing=programs.advance_program(pid,user_id=owner)
        assert diagnosing['current']['step']['kind']=='replan' and len(tasks)==2


from tests.evolution.test_evolution_background_execution import command as _background_command_fixture
background_command = _background_command_fixture


@pytest.mark.parametrize('legacy', [False, True])
def test_late_background_refutation_enters_diagnosis_once(task_runtime,background_command,legacy):
    from lib.evolution import programs
    from tests.evolution.test_evolution_scientific_observations import protocol
    from tests.evolution.test_evolution_background_execution import attach_background_launch
    from tests.evolution.test_evolution_runs import OWNER as owner
    from tests.support.research_observations import observation_payload
    from lib.tasks_pkg import background_commands
    from lib.project_mod.run_command import _format_run_output
    _,store,tasks=task_runtime;frozen=protocol()
    initial=programs.create_program({'objective':'Find an independently measured improvement','steps':[
        {**step(),'scientific_protocol':frozen,'scientific_required_outcome':'supported'},step('final')]},
        user_id=owner,idempotency_key='late-negative-replan')
    pid=initial['program_id'];programs.command_program(pid,{'action':'start'},user_id=owner)
    attach_background_launch(background_command,tasks[0])
    waiting=finish_task(task_runtime,tasks[0],'Measurement is still running.')
    assert waiting['background_wait'] and len(tasks)==1
    if legacy:
        from lib.evolution import stage_outcomes
        store.rows[(stage_outcomes.NAMESPACE, stage_outcomes._key(tasks[0]['id'], owner))]['scientific_audit']['audit_version'] -= 1
    unresolved=programs.advance_program(pid,user_id=owner)
    assert unresolved['queue']==initial['queue'] and unresolved['current']
    handle=background_command[1]
    background_commands.finish_handed_off_command(command_id=handle,result=_format_run_output(
        'experiment',json.dumps(observation_payload(frozen,'metrics','refuted')),'',0),error=None)
    settled=programs.advance_program(pid,user_id=owner)
    assert settled['status'] == ('needs_attention' if legacy else 'active')
    assert settled['queue'] == (initial['queue'] if legacy else [programs.replanning.diagnosis_step(1)] + initial['queue'])
    assert len(tasks) == 1
    receipt=programs.experiment_page(pid,user_id=owner)['experiments'][0]
    assert receipt['error'] == ('scientific_observations_unverified' if legacy else 'scientific_outcome_requirement_unmet')
    assert receipt['background_evidence_source']
    assert len(programs.experiment_page(pid,user_id=owner)['experiments'])==1


def test_error_label_or_consistent_supported_audit_cannot_authorize_negative_diagnosis():
    from lib.evolution.replanning import definitive_failure
    from lib.evolution.tool_evidence import scientific_audit
    from tests.evolution.test_evolution_scientific_observations import protocol,command as measured_command
    from tests.support.research_observations import observation_payload
    frozen=protocol()
    run={'status':'failed','error':'scientific_outcome_requirement_unmet','review':None,
        'scientific_protocol':frozen,'scientific_required_outcome':'supported'}
    assert not definitive_failure(run)
    audit=scientific_audit(measured_command(observation_payload(frozen,'metrics','supported')),frozen)
    assert not definitive_failure(run,scientific_audit=audit)
    audit=scientific_audit(measured_command(observation_payload(frozen,'metrics','refuted')),frozen)
    assert definitive_failure(run,scientific_audit=audit)
    assert not definitive_failure({**run,'scientific_protocol':{**frozen,'primary_metric':'foreign'}},scientific_audit=audit)
    assert not definitive_failure({**run,'scientific_required_outcome':None},scientific_audit=audit)

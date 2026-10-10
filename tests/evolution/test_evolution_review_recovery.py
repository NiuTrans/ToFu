"""Confirmed process death, read-only successor authority and incomplete cost truth."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from lib.storage.errors import StorageError
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER

pytestmark = pytest.mark.unit


def reviewing(runtime, **options):
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Explain safe research handoffs.', 'max_attempts': 1, **options}, user_id=OWNER)
    tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'observed-command', 'content': 'Actual handoff trace: 42 independent receipts.'})
    return finish(runtime, tasks[0], 'Safe handoffs preserve the evidence and original objective.')


def hide(runtime, monkeypatch, *task_ids, state='dead'):
    from lib.evolution import review_recovery
    runs, _, tasks = runtime
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id: next(
        (t for t in tasks if t['id'] == tid and tid not in task_ids and t['_userId'] == user_id), None))
    monkeypatch.setattr(review_recovery, 'owner_state', lambda owner: state)


def advance(runtime, run):
    return runtime[0].advance_run(run['run_id'], user_id=OWNER)


def test_confirmed_lost_review_recovers_exact_evidence_and_preserves_episode(runtime, monkeypatch):
    from lib.evolution import get_episode
    run = reviewing(runtime); old_prompt = runtime[2][-1]['messages'][0]['content']
    hide(runtime, monkeypatch, run['task_id'])
    prepared = advance(runtime, run)
    assert prepared['status'] == 'preparing' and len(runtime[2]) == 2
    assert prepared['stage_losses'][0]['admitted_recovery'] and prepared['actuals_complete'] is False
    restored = advance(runtime, prepared)
    assert len(runtime[2]) == 3 and restored['episode_id'] == run['episode_id']
    assert restored['attempt'] == 1 and restored['task_id'] != run['task_id']
    assert runtime[2][-1]['messages'][0]['content'] == old_prompt
    assert '42 independent receipts' in old_prompt
    assert runtime[2][-1]['config']['executionLimits']['max_tool_calls'] == 0
    completed = finish(runtime, runtime[2][-1], review())
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 30
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'success' and episode['actuals_complete'] is False
    assert episode['actuals']['tokens'] == 30
    assert len(completed['receipts']) == 2  # lost review is not a fabricated usage receipt


@pytest.mark.parametrize('state', ['alive', 'unknown'])
def test_registry_miss_without_process_death_never_replays(runtime, monkeypatch, state):
    run = reviewing(runtime); hide(runtime, monkeypatch, run['task_id'], state=state)
    attention = advance(runtime, run)
    assert attention['status'] == 'needs_attention' and 'task_unavailable' in attention['error']
    assert attention['actuals_complete'] is False and len(runtime[2]) == 2


@pytest.mark.parametrize('case', ['tokens', 'disabled', 'deadline', 'missing_archive'])
def test_recovery_refusals_close_aborted_episode_without_verdict_or_false_cost(runtime, monkeypatch, case):
    from lib.evolution import get_episode, stage_launch
    options = {'budget': {'max_tokens': 1000}} if case == 'tokens' else {'max_review_recoveries': 0} if case == 'disabled' else {}
    run = reviewing(runtime, **options)
    if case == 'deadline':
        stored, version = runtime[0]._read(run['run_id'], OWNER)
        stored['absolute_deadline_at'] = 1;runtime[0]._save(stored, version, OWNER)
    elif case == 'missing_archive':
        key = stage_launch.records.entity_key(run['stage_launch']['intent_id'], OWNER, kind='stage launch')
        runtime[1].rows.pop((stage_launch.NAMESPACE, key))
    hide(runtime, monkeypatch, run['task_id'])
    attention = advance(runtime, run)
    assert attention['status'] == 'needs_attention' and attention['error'].startswith('stage_loss_recovery_refused:')
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['verdict'] is None
    assert episode['actuals_complete'] is False and episode['actuals']['tokens'] == 15
    assert len(runtime[2]) == 2


def test_two_recovery_limit_counts_committed_attempts_and_is_episode_scoped(runtime, monkeypatch):
    run = reviewing(runtime); lost = []
    for _ in range(2):
        lost.append(run['task_id']);hide(runtime, monkeypatch, *lost)
        prepared = advance(runtime, run);run = advance(runtime, prepared)
        assert run['status'] == 'reviewing'
    lost.append(run['task_id']);hide(runtime, monkeypatch, *lost)
    attention = advance(runtime, run)
    assert attention['status'] == 'needs_attention' and 'retry_budget' in attention['error']
    assert len(runtime[2]) == 4 and len(attention['stage_losses']) == 3
    assert attention['stage_losses'][-1]['admitted_recovery'] is False


def test_loss_marker_ack_failure_is_idempotent_and_sweep_resumes(runtime, monkeypatch):
    run = reviewing(runtime);hide(runtime, monkeypatch, run['task_id'])
    original = runtime[0]._save
    def lost_ack(value, version, user_id):
        result = original(value, version, user_id)
        if value.get('stage_losses'): raise StorageError('database_timeout', 'ack lost after commit')
        return result
    monkeypatch.setattr(runtime[0], '_save', lost_ack)
    with pytest.raises(StorageError): advance(runtime, run)
    monkeypatch.setattr(runtime[0], '_save', original)
    runtime[0].sweep_stale_runs(user_id=OWNER, grace_seconds=0)
    restored = runtime[0].get_run(run['run_id'], user_id=OWNER)
    assert restored['status'] == 'reviewing' and len(restored['stage_losses']) == 1
    assert len(runtime[2]) == 3
    advance(runtime, restored);assert len(runtime[2]) == 3


def test_cancel_pending_recovery_closes_known_episode_and_launches_no_successor(runtime, monkeypatch):
    from lib.evolution import get_episode
    run = reviewing(runtime);hide(runtime, monkeypatch, run['task_id'])
    prepared = advance(runtime, run)
    cancelled = runtime[0].cancel_run(run['run_id'], user_id=OWNER)
    advance(runtime, cancelled)
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['actuals_complete'] is False
    assert episode['actuals']['tokens'] == 15 and len(runtime[2]) == 2


def test_dead_executor_is_not_replayed_and_does_not_reuse_previous_episode_cost(runtime, monkeypatch):
    from lib.evolution import get_episode
    run = reviewing(runtime, max_attempts=2)
    repair = finish(runtime, runtime[2][-1], review('fail'))
    previous = get_episode(run['episode_id'], user_id=OWNER)
    assert previous['actuals']['tokens'] == 30
    hide(runtime, monkeypatch, repair['task_id'])
    stopped = advance(runtime, repair)
    assert stopped['status'] == 'needs_attention' and 'executor_outcome_unknown' in stopped['error']
    current = get_episode(repair['episode_id'], user_id=OWNER)
    assert current['status'] == 'aborted' and current['actuals']['tokens'] == 0
    assert current['actuals_complete'] is False and len(runtime[2]) == 3


def test_following_repair_episode_has_complete_actuals_despite_prior_lost_review(runtime, monkeypatch):
    from lib.evolution import get_episode
    run = reviewing(runtime, max_attempts=2);hide(runtime, monkeypatch, run['task_id'])
    restored = advance(runtime, advance(runtime, run))
    repair = finish(runtime, runtime[2][-1], review('fail'))
    assert get_episode(restored['episode_id'], user_id=OWNER)['actuals_complete'] is False
    judging = finish(runtime, runtime[2][-1], 'Repaired deliverable with actual verification.')
    completed = finish(runtime, runtime[2][-1], review())
    episode = get_episode(completed['episode_id'], user_id=OWNER)
    assert episode.get('actuals_complete', True) and episode['actuals']['tokens'] == 30
    assert completed['actuals_complete'] is False and completed['actuals']['tokens'] == 60


def test_process_probe_distinguishes_a_live_then_exited_owned_process():
    from lib.evolution.review_recovery import process_owner, owner_state
    if process_owner() is None: pytest.skip('local process evidence unavailable')
    code = 'from lib.evolution.review_recovery import process_owner;import json,sys;print(json.dumps(process_owner()),flush=True);sys.stdin.readline()'
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        owner = json.loads(child.stdout.readline())
        assert owner_state(owner) == 'alive'
        child.stdin.write('\n');child.stdin.flush();child.wait(timeout=5)
        assert owner_state(owner) == 'dead'
        assert owner_state({**owner, 'boot': 'another boot'}) == 'unknown'
        assert owner_state({**process_owner(), 'started': '0'}) == 'dead'  # reused PID, different start
    finally:
        if child.poll() is None: child.kill();child.wait(timeout=5)


def test_cancel_after_dead_executor_closes_an_empty_but_incomplete_episode(runtime, monkeypatch):
    from lib.evolution import get_episode
    run = runtime[0].submit_run({'objective': 'Inspect handoff evidence.'}, user_id=OWNER)
    hide(runtime, monkeypatch, run['task_id'])
    cancelled = runtime[0].cancel_run(run['run_id'], user_id=OWNER)
    settled = advance(runtime, cancelled)
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert settled['status'] == 'cancelled' and episode['status'] == 'aborted'
    assert episode['actuals_complete'] is False and episode['actuals']['tokens'] == 0
    assert len(runtime[2]) == 1


def test_incomplete_consumption_excludes_cost_self_model_and_training_samples(runtime, monkeypatch):
    from lib.evolution import cost_model, self_model, distill
    run = reviewing(runtime);hide(runtime, monkeypatch, run['task_id'])
    restored = advance(runtime, advance(runtime, run))
    completed = finish(runtime, runtime[2][-1], review())
    priors = cost_model.recompute_priors(user_id=OWNER)
    assert all(prior['samples'] == 0 for prior in priors)
    assert self_model._episode_rows(user_id=OWNER) == []
    # Even a checker-produced verdict cannot turn the lower bound into a full
    # trajectory cost. The record is synthetic only at the expensive checker seam.
    from lib.evolution import records, get_episode
    episode = get_episode(completed['episode_id'], user_id=OWNER)
    episode['verdict']['assessed_by'] = 'checker'
    records.put_record(records.NAMESPACE_EPISODES, records.entity_key(episode['episode_id'], OWNER, kind='episode'), episode, kind='episode')
    assert distill.build_trajectory_dataset(user_id=OWNER, domain='general')['samples'] == 0


def test_incomplete_actuals_refuse_efficiency_learning_and_reports_label_lower_bounds(runtime):
    from lib.evolution import settle_episode, get_episode, render_goal_report
    run = reviewing(runtime)
    with pytest.raises(runtime[0].EvolutionStateError, match='incomplete actuals'):
        settle_episode(run['episode_id'], user_id=OWNER, status='aborted', actuals=run['actuals'], actuals_complete=False,
                       learning={'resource_weight_deltas': [], 'cost_actual_vs_estimate': {'tokens_ratio': 0.1, 'wall_clock_ratio': 0.1},
                                 'strategy_outcome': None, 'capability_gap': None})
    assert get_episode(run['episode_id'], user_id=OWNER)['status'] == 'running'
    settle_episode(run['episode_id'], user_id=OWNER, status='aborted', actuals=run['actuals'], actuals_complete=False)
    assert 'observed lower bound; consumption incomplete' in render_goal_report(run['goal_id'], user_id=OWNER)


def test_competing_recovery_admissions_bind_only_one_successor(runtime, monkeypatch):
    import threading
    runs, _, tasks = runtime
    run = reviewing(runtime);hide(runtime, monkeypatch, run['task_id'])
    prepared = advance(runtime, run)
    from lib.tasks_pkg import manager
    original = manager.create_task;barrier = threading.Barrier(2)
    def raced(*args, **kwargs):
        task = original(*args, **kwargs);barrier.wait(timeout=5);return task
    monkeypatch.setattr(manager, 'create_task', raced)
    def attempt():
        try:return advance(runtime, prepared)
        except StorageError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:attempt(),range(2)))
    assert all(isinstance(result, dict) for result in results)
    assert len(tasks) == 3
    latest = runs.get_run(run['run_id'], user_id=OWNER)
    assert latest['status'] == 'reviewing' and len(latest['stage_losses']) == 1
    assert latest['episode_id'] == run['episode_id']
    assert all(result['task_id'] == latest['task_id'] for result in results)


def test_lost_review_with_finite_tool_allowance_never_allocates_another_worker(runtime, monkeypatch):
    from lib.evolution import get_episode
    run = reviewing(runtime, budget={'max_tokens': 0, 'max_tool_calls': 200})
    hide(runtime, monkeypatch, run['task_id'])
    stopped = advance(runtime, run)
    assert stopped['status'] == 'needs_attention'
    assert 'unobserved_tool_consumption' in stopped['error']
    assert len(runtime[2]) == 2
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['actuals_complete'] is False
    assert episode['actuals']['tokens'] == 15


def test_program_lost_executor_cannot_refresh_a_finite_tool_allowance(runtime, monkeypatch):
    from lib.evolution import programs
    program = programs.create_program({'objective': 'Retain a finite cumulative tool allowance across process loss.',
        'auto_replan': False, 'budget': {'max_tokens': 0, 'max_tool_calls': 200}},
        user_id=OWNER, idempotency_key='lost-executor-tool-budget')
    program = programs.command_program(program['program_id'], {'action': 'start'}, user_id=OWNER)
    task_id = runtime[2][0]['id']
    hide(runtime, monkeypatch, task_id)
    stopped = programs.advance_program(program['program_id'], user_id=OWNER)
    assert stopped['status'] == 'needs_attention' and stopped['actuals_complete'] is False
    assert 'executor_outcome_unknown' in stopped['error']
    before = deepcopy(stopped)
    writes = runtime[1].writes
    with pytest.raises(programs.EvolutionStateError, match='program tool consumption is unknown'):
        programs.command_program(program['program_id'], {'action': 'retry'}, user_id=OWNER)
    assert programs.get_program(program['program_id'], user_id=OWNER) == before
    assert runtime[1].writes == writes and len(runtime[2]) == 1


def test_scientific_review_recovery_keeps_original_executor_episode_and_attempt(runtime, monkeypatch):
    from tests.evolution.test_evolution_scientific_observations import protocol, command
    from tests.support.research_observations import observation_payload
    from lib.evolution.source_tools import scientific_review_binding_matches
    from lib.evolution.stage_launch import read_launch
    from lib.evolution.stage_outcomes import read_metadata
    runs, _, tasks = runtime
    frozen = protocol()
    run = runs.submit_run({'objective':'Recover independent review of the frozen experiment.',
        'scientific_protocol':frozen,'scientific_required_outcome':'supported','max_attempts':1},user_id=OWNER)
    executor = tasks[-1]
    executor.update(command(observation_payload(frozen,'metrics','supported')))
    reviewing_run = finish(runtime,executor,'Observed supported measurement.')
    hide(runtime,monkeypatch,reviewing_run['task_id'])
    prepared = advance(runtime,reviewing_run)
    restored = advance(runtime,prepared)
    assert restored['status'] == 'reviewing' and len(tasks) == 3
    assert restored['episode_id'] == reviewing_run['episode_id']
    assert restored['attempt'] == reviewing_run['attempt']
    intent = read_launch(restored['stage_launch'],run_id=restored['run_id'],user_id=OWNER)
    assert intent['source_task_id'] == executor['id']
    metadata = read_metadata(restored['run_id'],executor['id'],user_id=OWNER)
    assert metadata['episode_id'] == restored['episode_id'] and metadata['attempt'] == restored['attempt']
    assert scientific_review_binding_matches(restored,owner=OWNER)
    assert len([task for task in tasks if task['config']['evolution']['stage'] == 'executing']) == 1


@pytest.mark.parametrize('change', ['threshold', 'required_outcome'])
def test_lost_scientific_review_refuses_changed_experiment_before_allocating_worker(runtime, monkeypatch, change):
    from tests.evolution.test_evolution_scientific_observations import protocol, command
    from tests.support.research_observations import observation_payload
    from lib.evolution import get_episode
    runs, _, tasks = runtime
    frozen = protocol()
    run = runs.submit_run({'objective': 'Recover only the original experiment review.',
        'scientific_protocol': frozen, 'scientific_required_outcome': 'supported',
        'max_attempts': 0}, user_id=OWNER)
    executor = tasks[-1]
    executor.update(command(observation_payload(frozen, 'metrics', 'supported')))
    run = finish(runtime, executor, 'Observed supported measurement.')
    stored, version = runs._read(run['run_id'], OWNER)
    if change == 'threshold':
        stored['scientific_protocol']['decision_rule']['minimum_effect'] += 1
    else:
        stored['scientific_required_outcome'] = 'refuted'
    runs._save(stored, version, OWNER)
    hide(runtime, monkeypatch, run['task_id'])
    settled = advance(runtime, run)
    assert settled['status'] == 'needs_attention'
    assert settled['error'].startswith('stage_loss_recovery_refused:')
    assert not settled['stage_losses'][-1]['admitted_recovery']
    assert len(tasks) == 2
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['verdict'] is None
    assert settled['actuals_complete'] is False

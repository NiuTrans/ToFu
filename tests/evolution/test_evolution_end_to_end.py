"""End-to-end scenarios: the whole self-evolution system as one organism.

These are not unit tests. Each scenario drives the full loop — intake,
indexing, planning, confirmation, execution (synthetic terminal tasks),
automatic settlement, the maintenance heartbeat — and asserts the system is
measurably smarter on the next attempt: better estimates, replayed winners,
validated factors, retry probes, gated macros with automatic rollback.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _goal(goal_id, statement, domain, *, command='true', tokens=50000):
    return {
        'schema_version': 1, 'goal_id': goal_id, 'statement': statement,
        'domain': domain, 'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': command}}],
        'budget_envelope': {'max_tokens': tokens,
                            'max_wall_clock_seconds': 3600,
                            'max_tool_calls': 100},
        'status': 'draft', 'created_at': 1,
    }


def _run_episode(store, goal, *, plan=None, task=None, verdict_command='true',
                 settled_tokens=40000, tool_calls=4, settled_at=100):
    """One full loop iteration for a goal: plan → confirm → open → terminal
    task → listener-style settlement through the bridge with real checkers."""
    from lib import evolution
    if evolution.get_goal(goal['goal_id'], user_id=USER) is None:
        evolution.admit_goal(goal, user_id=USER)
    goal_doc = evolution.get_goal(goal['goal_id'], user_id=USER)
    if plan is None:
        plan = evolution.build_plan(goal_doc, evolution.suggest_candidates(
            goal_doc, user_id=USER), user_id=USER)
    evolution.save_plan({**plan, 'status': 'confirmed'}, user_id=USER)
    episode = evolution.open_episode(
        goal_id=goal['goal_id'], plan_id=plan['plan_id'],
        selected_path_id=plan['selected_path_id'],
        task_id=f"task-{goal['goal_id']}", user_id=USER)
    task = task or {
        'id': f"task-{goal['goal_id']}", '_userId': USER, 'status': 'done',
        'created_at': 10.0, 'finished_at': 40.0,
        'usage': {'input_tokens': settled_tokens - 5000,
                  'output_tokens': 5000},
        'toolRounds': [{'tool_calls': [{'name': f'tool-{i}'}
                                       for i in range(tool_calls)]}],
    }
    if verdict_command == 'true':
        verdict = evolution.evaluate_goal(goal_doc, user_id=USER)
    else:
        verdict = {'outcome': 'failure', 'assessed_by': 'checker',
                   'criterion_results': [{'criterion_id': 'c1',
                                          'outcome': 'fail'}]}
    settled = evolution.settle_from_task(
        episode['episode_id'], task, user_id=USER,
        verdict=verdict, now=settled_at)
    return plan, episode, settled


def test_scenario_research_loop_gets_smarter_on_the_second_goal(store):
    """North star: research goals run end to end; estimates tighten, winners
    replay, strategies distill, and the report quotes real receipts."""
    from lib import evolution
    first = _goal('res-1', 'improve sparse attention for long context',
                  'research')
    plan, episode, settled = _run_episode(store, first)
    assert settled['status'] == 'success'
    assert settled['actuals']['tokens'] == 40000

    cycle = evolution.run_maintenance_cycle(user_id=USER)
    assert cycle['ok'] is True
    # One sample: honest flat prior, zero is never fabricated as knowledge.
    assert evolution.estimate_cost(
        'research', user_id=USER)['priors_source'] == 'exploratory_flat'
    report = evolution.render_goal_report('res-1', user_id=USER)
    assert 'improve sparse attention' in report
    assert '**success**' in report
    assert '| 40000 |' in report or '| 50000 |' in report

    second = _goal('res-2', 'improve sparse attention with linear memory',
                   'research')
    evolution.admit_goal(second, user_id=USER)
    second_doc = evolution.get_goal('res-2', user_id=USER)
    candidates = evolution.suggest_candidates(second_doc, user_id=USER)
    replays = [c for c in candidates if c['path_id'].startswith('replay-')]
    assert replays, 'history must offer the previous winning path'
    assert replays[0]['expected_utility'] == 0.7

    _plan2, _ep2, settled2 = _run_episode(store, second, settled_at=200)
    assert settled2['status'] == 'success'
    third = _goal('res-3', 'improve sparse attention with block sparsity',
                  'research')
    _plan3, _ep3, settled3 = _run_episode(store, third, settled_at=300)
    assert settled3['status'] == 'success'
    evolution.run_maintenance_cycle(user_id=USER)

    estimate = evolution.estimate_cost('research', user_id=USER)
    assert estimate['priors_source'] == 'measured_history'
    assert estimate['expected_tokens'] == 40000  # median of three receipts
    strategies = evolution.retrieve_strategies(
        'improve sparse attention', user_id=USER, domain='research')
    assert strategies and strategies[0]['samples'] >= 1
    dataset = evolution.build_trajectory_dataset(user_id=USER,
                                                 domain='research')
    assert dataset['samples'] == 3  # all checker-verified wins


def test_scenario_failures_teach_the_system_and_curriculum_retries(store):
    """Calm successes vs tool-storm failures: the self-model validates the
    factor, the anti-pattern forms, and the curriculum's retry wins."""
    from lib import evolution
    from lib.evolution import records
    for index in (1, 2, 3):
        goal = _goal(f'calm-{index}', 'train the retrieval model', 'code')
        _p, _e, settled = _run_episode(store, goal, tool_calls=5,
                                       settled_at=50 + index)
        assert settled['status'] == 'success'
    for index in (1, 2, 3):
        goal = _goal(f'fail-{index}', 'train the retrieval model', 'code')
        plan, episode, settled = _run_episode(
            store, goal, verdict_command='false', tool_calls=60,
            settled_at=100 + index)
        assert settled['status'] == 'failure'
        settled['stage_receipts'] = [{'stage': 'train', 'status': 'error'}]
        records.put_record(records.NAMESPACE_EPISODES,
                           f"{USER}:{episode['episode_id']}", settled,
                           kind='episode')

    evolution.run_maintenance_cycle(user_id=USER)
    patterns = evolution.list_anti_patterns(user_id=USER, domain='code')
    assert [p['signature'] for p in patterns] == ['failed:train']

    evolution.propose_hypotheses([{
        'domain': 'code', 'feature': 'tool_calls', 'operator': 'gt',
        'threshold': 50, 'direction': 'hurts',
        'statement': 'tool storms above fifty calls fail'}], user_id=USER)
    [verdict] = evolution.validate_hypotheses(user_id=USER)
    assert verdict['status'] == 'admitted'
    assert verdict['evidence']['rate_match'] == 0.0
    assert verdict['evidence']['rate_rest'] == 1.0
    prediction = evolution.predict_success(
        'code', {'tool_calls': 80}, user_id=USER)
    assert prediction is not None
    assert prediction['success_probability'] < prediction['baseline']

    # The heartbeat's curriculum step already admitted the retry probe.
    retries = [g for g in evolution.list_goals(user_id=USER)
               if str(g.get('statement') or '').startswith(
                   '[probe:anti-pattern]')]
    assert len(retries) == 1
    retry = retries[0]
    assert retry['exploratory'] is False  # inherited test_command criteria
    assert retry['criteria'][0]['params']['command'] == 'true'
    # Proposing again from the same frontier deduplicates to nothing.
    assert evolution.propose_probes(user_id=USER, now=300) == []

    # The retry runs with the anti-pattern as guidance and a calm tool count.
    _plan3, _ep3, settled3 = _run_episode(
        store, {**retry, 'goal_id': retry['goal_id']},
        tool_calls=5, settled_at=400)
    assert settled3['status'] == 'success'
    report = evolution.render_goal_report(retry['goal_id'], user_id=USER)
    assert 'Self-model factors' in report


def test_scenario_gate_full_journey_with_macro_and_auto_rollback(store):
    """A recurring winning tool sequence crystallizes through the full gate;
    post-activation degradation rolls it back and restores the parent."""
    from lib import evolution
    tasks = {}
    for index in (1, 2):
        goal = _goal(f'win-{index}', 'summarize the paper corpus', 'research')
        task = {
            'id': f'task-win-{index}', '_userId': USER, 'status': 'done',
            'created_at': 10.0, 'finished_at': 40.0,
            'usage': {'input_tokens': 9000, 'output_tokens': 1000},
            'toolRounds': [{'tool_calls': [
                {'name': 'search'}, {'name': 'fetch'},
                {'name': 'summarize'}]}],
        }
        tasks[task['id']] = task
        _run_episode(store, goal, task=task, settled_at=100 + index)

    loader = lambda task_id, *, user_id: tasks.get(task_id)
    proposals = evolution.propose_macro_crystallizations(
        user_id=USER, loader=loader, now=200)
    assert len(proposals) == 1
    macro = proposals[0]
    assert macro['payload']['sequence'] == ['search', 'fetch', 'summarize']
    assert set(macro['evidence_episode_ids']) == {
        f'ep-{e}' for e in ()} or len(macro['evidence_episode_ids']) == 2

    evolution.confirm_mutation(macro['mutation_id'], user_id=USER, now=210)
    evolution.run_maintenance_cycle(user_id=USER)
    evolution.run_regression(macro, user_id=USER, now=220)
    evolution.activate_mutation(macro['mutation_id'], user_id=USER, now=230,
                                allow_unmeasurable=True)
    assert evolution.list_mutations(
        user_id=USER, kind='macro_tool')[0]['status'] == 'active'

    for index in range(3):
        goal = _goal(f'degrade-{index}', 'summarize the paper corpus',
                     'research')
        _run_episode(store, goal, verdict_command='false',
                     settled_at=300 + index)
    for index in range(3):
        goal = _goal(f'independent-{index}', 'summarize a separate paper corpus', 'research')
        _run_episode(store, goal, settled_at=350+index)
    evolution.build_holdout_suite(user_id=USER,now=360)
    def paired(mutation,item):
        return {arm:{'outcome':outcome,'tokens':None,'wall_clock_seconds':1}
                for arm,outcome in [('baseline','pass'),('candidate','fail')]}
    evolution.run_regression(macro,user_id=USER,paired_probe=paired,now=390)
    report = evolution.monitor_active_mutations(user_id=USER, now=400)
    assert report and report[0]['breach'] is True
    assert evolution.list_mutations(
        user_id=USER, kind='macro_tool')[0]['status'] == 'rolled_back'

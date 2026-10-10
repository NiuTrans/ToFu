"""System coherence: one heartbeat, one report, evolvable gate, policy programs."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settle(store, goal_id, episode_id, outcome, *, domain='code',
            tokens=100, tool_calls=1, evidence='checker', statement=None):
    from lib import evolution
    from lib.evolution import records
    if evolution.get_goal(goal_id, user_id=USER) is None:
        evolution.admit_goal({
            'schema_version': 1, 'goal_id': goal_id,
            'statement': statement or f'goal in {domain}', 'domain': domain,
            'exploratory': False,
            'criteria': [{'criterion_id': 'c', 'kind': 'test_command',
                          'params': {'command': 'true'}}],
            'budget_envelope': {'max_tokens': 99999,
                                'max_wall_clock_seconds': 9999,
                                'max_tool_calls': 999},
            'status': 'draft', 'created_at': 1}, user_id=USER)
        evolution.save_plan({
            'schema_version': 1, 'plan_id': f'plan-{goal_id}',
            'goal_id': goal_id, 'plan_revision': 1,
            'candidates': [{
                'path_id': 'path-a', 'title': 'Alpha path',
                'stages': [{'kind': 'execute', 'detail': 'd'}],
                'resource_refs': [],
                'cost_estimate': {
                    'expected_tokens': tokens, 'expected_wall_clock_seconds': 10,
                    'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                    'expected_information_gain': 0.5, 'expected_utility': 0.9,
                    'priors_source': 'exploratory_flat'},
                'utility_over_cost': 0.9},
                {'path_id': 'path-b', 'title': 'Beta path',
                 'stages': [{'kind': 'execute', 'detail': 'd'}],
                 'resource_refs': [],
                 'cost_estimate': {
                     'expected_tokens': tokens, 'expected_wall_clock_seconds': 10,
                     'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                     'expected_information_gain': 0.5, 'expected_utility': 0.1,
                     'priors_source': 'exploratory_flat'},
                 'utility_over_cost': 0.1}],
            'selected_path_id': 'path-a', 'selection_rationale': 'r',
            'requires_confirmation': True, 'status': 'confirmed',
            'created_at': 1}, user_id=USER)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': goal_id,
        'plan_id': f'plan-{goal_id}', 'selected_path_id': 'path-a',
        'exploratory': False, 'task_id': f't-{episode_id}', 'status': outcome,
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': tokens, 'wall_clock_seconds': 10,
                    'tool_calls': tool_calls},
        'verdict': {'outcome': outcome, 'assessed_by': evidence,
                    'criterion_results': [], 'evidence_refs': ['task:1']},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def test_maintenance_cycle_runs_every_pass_in_order(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    report = evolution.run_maintenance_cycle(user_id=USER)
    assert list(report['steps']) == [
        'mutation_recovery', 'priors', 'hypotheses', 'strategies', 'capability_gaps', 'friction',
        'refetch', 'frame_proposals', 'holdout', 'crystallizations',
        'flow_crystallizations', 'curriculum', 'monitoring']
    assert report['ok'] is True
    assert report['steps']['priors']['result']


def test_goal_report_quotes_receipts_and_never_invents(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success', tokens=123,
            statement='improve sparse attention')
    text = evolution.render_goal_report('g1', user_id=USER)
    assert 'improve sparse attention' in text
    assert '| 123 | 123 |' in text  # estimate vs actual from receipts
    assert '**success** (by checker)' in text
    assert 'evidence: task:1' in text
    assert evolution.render_goal_report('missing', user_id=USER) == ''


def test_gate_thresholds_are_evolvable_through_the_gate(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    assert evolution.get_thresholds(user_id=USER)['min_lift'] == 0.15
    mutation = evolution.propose_mutation(
        'budget_policy', {'policy': '{"min_lift": 0.4}'}, ['e1'],
        user_id=USER)
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    evolution.build_holdout_suite(user_id=USER)
    evolution.run_regression(mutation, user_id=USER,
                             probe=lambda m, item: 'pass')
    evolution.activate_mutation(mutation['mutation_id'], user_id=USER)
    assert evolution.get_thresholds(user_id=USER)['min_lift'] == 0.4
    with pytest.raises(evolution.EvolutionStateError):
        bad = evolution.propose_mutation(
            'budget_policy', {'policy': '{"min_lift": 9}'}, ['e1'],
            user_id=USER)
        evolution.confirm_mutation(bad['mutation_id'], user_id=USER)
    report = evolution.meta_report(user_id=USER)
    assert report['proposed'] >= 2
    assert report['accepted_rate'] is not None


POLICY_CODE = '''def score(candidate, context):
    """Prefer the path whose title mentions alpha (fixture policy)."""
    bonus = 1.0 if 'alpha' in (candidate.get('title') or '').lower() else 0.0
    return float(candidate.get('utility_over_cost') or 0.0) + bonus
'''

POLICY_TESTS = '''from skill_impl import score


def test_score_prefers_alpha():
    candidate = {'title': 'alpha path', 'utility_over_cost': 0.5}
    assert score(candidate, {}) == 1.5


def test_score_neutral_otherwise():
    assert score({'title': 'beta', 'utility_over_cost': 0.5}, {}) == 0.5
'''


def test_policy_program_governs_ranking_after_gate_install(store, monkeypatch):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    monkeypatch.setattr(
        'lib.evolution.skill_forge._install_via_lib_skills',
        lambda skill, *, project_path, user_id: None)
    policy = evolution.forge_policy(
        'prefer alpha-flavored paths', user_id=USER,
        author=lambda req: {'code': POLICY_CODE, 'tests': POLICY_TESTS})
    assert policy['name'].startswith('planner-policy')
    sandboxed = evolution.sandbox_run(policy['skill_id'], user_id=USER)
    assert sandboxed['status'] == 'sandboxed'
    mutation = evolution.propose_skill(policy['skill_id'], user_id=USER,
                                       evidence_episode_ids=['e1'])
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    evolution.build_holdout_suite(user_id=USER)
    receipt = evolution.run_regression(mutation, user_id=USER)
    assert receipt['verdict'] in ('pass', 'unmeasurable')
    evolution.activate_mutation(mutation['mutation_id'], user_id=USER,
                                allow_unmeasurable=True)
    evolution.install_skill(policy['skill_id'], user_id=USER)
    loaded = evolution.load_active_policy(user_id=USER)
    assert callable(loaded)
    goal = evolution.get_goal('g1', user_id=USER)
    plan = evolution.build_plan(goal, [
        {'path_id': 'beta', 'title': 'beta path', 'expected_utility': 0.9,
         'stages': [{'kind': 'execute', 'detail': 'd'}]},
        {'path_id': 'alpha', 'title': 'alpha path', 'expected_utility': 0.1,
         'stages': [{'kind': 'execute', 'detail': 'd'}]},
    ], user_id=USER, persist=False)
    assert plan['selected_path_id'] == 'alpha'  # policy outranks heuristics


def test_policy_fixture_rejects_non_score_code(store):
    from lib import evolution
    bad = evolution.forge_policy(
        'broken policy', user_id=USER,
        author=lambda req: {
            'code': 'def unrelated():\n    return 1\n',
            'tests': ('from skill_impl import unrelated\n\n\n'
                      'def test_ok():\n    assert unrelated() == 1\n')})
    rejected = evolution.sandbox_run(bad['skill_id'], user_id=USER)
    assert rejected['status'] == 'rejected'
    # pytest passed on its own terms; the policy fixture is the rejector.
    assert 'score' in rejected['sandbox_receipt']['output']


def test_distillation_floor_and_proposal(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    _settle(store, 'g2', 'e2', 'success', evidence='human')  # excluded
    _settle(store, 'g3', 'e3', 'failure')                     # excluded
    dataset = evolution.build_trajectory_dataset(user_id=USER, domain='code')
    assert dataset['samples'] == 1
    assert dataset['episode_ids'] == ['e1']
    assert dataset['verdict_floor'] == 'checker'
    with pytest.raises(evolution.EvolutionStateError):
        evolution.propose_distilled_model(dataset, 'local/tuned-1',
                                          evidence_episode_ids=['e1'],
                                          user_id=USER)
    trained = []
    mutation = evolution.propose_distilled_model(
        dataset, 'local/tuned-1', evidence_episode_ids=['e1'], user_id=USER,
        trainer=lambda ds, ref: trained.append(ref))
    assert trained == ['local/tuned-1']
    assert mutation['kind'] == 'routing_change'
    assert mutation['payload']['model'] == 'local/tuned-1'


def test_terminal_learning_defers_source_fetch_without_claiming_complete_work(store, monkeypatch):
    from lib.evolution import maintenance
    calls = []
    monkeypatch.setattr(maintenance, 'reindex_maintenance',
                        lambda **kw: calls.append(kw) or {'settle_triggered': 0, 'freshness': 0})
    deferred = maintenance.run_maintenance_cycle(user_id=USER, include_refetch=False)
    assert calls == [] and deferred['ok'] is True and deferred['complete'] is False
    assert deferred['steps']['refetch']['result']['deferred'] is True
    explicit = maintenance.run_maintenance_cycle(user_id=USER)
    assert calls == [{'user_id': USER}]
    assert explicit['complete'] is True and explicit['steps']['refetch']['result']['freshness'] == 0

"""Self-model (R4): hypotheses are cheap, data is the judge, planning changes."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settle(store, goal_id, episode_id, outcome, *, domain='code',
            tokens=100, tool_calls=1, wall=10, history_refs=0):
    from lib import evolution
    from lib.evolution import records
    if evolution.get_goal(goal_id, user_id=USER) is None:
        evolution.admit_goal({
            'schema_version': 1, 'goal_id': goal_id,
            'statement': f'goal in {domain}', 'domain': domain,
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
                'path_id': 'path-a', 'title': 'P',
                'stages': [{'kind': 'execute', 'detail': 'd'}],
                'resource_refs': [
                    {'ref_id': f'episode:h{i}', 'indexer': 'history',
                     'locator': f'h{i}'} for i in range(history_refs)],
                'cost_estimate': {
                    'expected_tokens': 10, 'expected_wall_clock_seconds': 10,
                    'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                    'expected_information_gain': 0.5, 'expected_utility': 0.5,
                    'priors_source': 'exploratory_flat'},
                'utility_over_cost': 1.0}],
            'selected_path_id': 'path-a', 'selection_rationale': 'r',
            'requires_confirmation': True, 'status': 'confirmed',
            'created_at': 1}, user_id=USER)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': goal_id,
        'plan_id': f'plan-{goal_id}', 'selected_path_id': 'path-a',
        'exploratory': False, 'task_id': f't-{episode_id}', 'status': outcome,
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': tokens, 'wall_clock_seconds': wall,
                    'tool_calls': tool_calls},
        'verdict': {'outcome': outcome, 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def _history_heavy_domain(store):
    """History-rich paths succeed; history-less paths fail. 4 vs 4."""
    for i in range(4):
        _settle(store, f'g-win-{i}', f'e-win-{i}', 'success', history_refs=2)
    for i in range(4):
        _settle(store, f'g-lose-{i}', f'e-lose-{i}', 'failure', history_refs=0)


def test_hypothesis_admitted_when_lift_clears_the_bar(store):
    from lib import evolution
    _history_heavy_domain(store)
    [hyp] = evolution.propose_hypotheses([{
        'domain': 'code', 'feature': 'has_history_resource',
        'operator': 'eq', 'threshold': True, 'direction': 'helps',
        'statement': 'paths citing local history succeed'}], user_id=USER)
    assert hyp['status'] == 'proposed'
    [verdict] = evolution.validate_hypotheses(user_id=USER)
    assert verdict['status'] == 'admitted'
    assert verdict['evidence']['rate_match'] == 1.0
    assert verdict['evidence']['rate_rest'] == 0.0
    assert verdict['evidence']['lift'] == 1.0


def test_wrong_direction_and_weak_effects_are_discarded(store):
    from lib import evolution
    _history_heavy_domain(store)
    evolution.propose_hypotheses([
        {'domain': 'code', 'feature': 'has_history_resource',
         'operator': 'eq', 'threshold': True, 'direction': 'hurts',
         'statement': 'claims the opposite of the data'},
        {'domain': 'code', 'feature': 'tokens', 'operator': 'gt',
         'threshold': 50, 'direction': 'helps',
         'statement': 'no real difference, all episodes share tokens=100'},
    ], user_id=USER)
    verdicts = evolution.validate_hypotheses(user_id=USER)
    by_statement = {v['statement']: v for v in verdicts}
    opposite = by_statement['claims the opposite of the data']
    assert opposite['status'] == 'discarded'
    assert opposite['evidence']['lift'] < 0
    # All episodes share tokens=100, so the rest group is empty: insufficient
    # data waits for a retest instead of forcing a verdict.
    no_difference = by_statement[
        'no real difference, all episodes share tokens=100']
    assert no_difference['status'] == 'proposed'


def test_insufficient_data_waits_instead_of_judging(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success', history_refs=1)
    evolution.propose_hypotheses([{
        'domain': 'code', 'feature': 'has_history_resource',
        'operator': 'eq', 'threshold': True, 'direction': 'helps',
        'statement': 'not enough data yet'}], user_id=USER)
    [verdict] = evolution.validate_hypotheses(user_id=USER)
    assert verdict['status'] == 'proposed'
    assert evolution.predict_success(
        'code', {'has_history_resource': True}, user_id=USER) is None


def test_uncomputable_feature_never_enters_the_queue(store):
    from lib import evolution
    saved = evolution.propose_hypotheses([
        {'feature': 'vibes', 'operator': 'gt', 'threshold': 3,
         'direction': 'helps', 'statement': 'untestable by construction'},
    ], user_id=USER)
    assert saved == []


def test_prediction_adjusts_planning_and_failure_prior(store):
    from lib import evolution
    _history_heavy_domain(store)
    evolution.recompute_priors(user_id=USER)
    evolution.propose_hypotheses([{
        'domain': 'code', 'feature': 'has_history_resource',
        'operator': 'eq', 'threshold': True, 'direction': 'helps',
        'statement': 'history helps'}], user_id=USER)
    evolution.validate_hypotheses(user_id=USER)

    with_history = evolution.predict_success(
        'code', {'has_history_resource': True}, user_id=USER)
    assert with_history is not None
    assert with_history['success_probability'] > with_history['baseline']

    evolution.admit_goal({
        'schema_version': 1, 'goal_id': 'new-goal',
        'statement': 'a new code goal', 'domain': 'code',
        'exploratory': False,
        'criteria': [{'criterion_id': 'c', 'kind': 'test_command',
                      'params': {'command': 'true'}}],
        'budget_envelope': {'max_tokens': 1000, 'max_wall_clock_seconds': 100,
                            'max_tool_calls': 10},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    goal = evolution.get_goal('new-goal', user_id=USER)
    plan = evolution.build_plan(goal, [
        {'path_id': 'with-history', 'title': 'replay local win',
         'stages': [{'kind': 'execute', 'detail': 'd'}],
         'resource_refs': [{'ref_id': 'episode:e-win-0', 'indexer': 'history',
                            'locator': 'e-win-0'}]},
        {'path_id': 'cold', 'title': 'from scratch',
         'stages': [{'kind': 'execute', 'detail': 'd'}],
         'resource_refs': []},
    ], user_id=USER, persist=False)
    by_id = {c['path_id']: c for c in plan['candidates']}
    warm = by_id['with-history']['cost_estimate']
    cold = by_id['cold']['cost_estimate']
    assert warm['expected_utility'] > cold['expected_utility']
    assert warm['failure_rate_prior'] < cold['failure_rate_prior']
    assert plan['selected_path_id'] == 'with-history'
    # Proposer judgment still wins when explicitly supplied.
    overridden = evolution.build_plan(goal, [
        {'path_id': 'judged', 'title': 'explicit utility',
         'stages': [{'kind': 'execute', 'detail': 'd'}],
         'resource_refs': [], 'expected_utility': 0.05},
    ], user_id=USER, persist=False)
    assert overridden['candidates'][0][
        'cost_estimate']['expected_utility'] == 0.05


def test_stage_signature_is_a_testable_feature(store):
    from lib import evolution
    from lib.evolution import records
    for index in range(3):
        _settle(store, f'gw{index}', f'ew{index}', 'success')
    for index in range(3):
        _settle(store, f'gl{index}', f'el{index}', 'failure')
    evolution.propose_hypotheses([{
        'domain': 'code', 'feature': 'stage_signature',
        'operator': 'eq', 'threshold': 'execute', 'direction': 'helps',
        'statement': 'single-stage execute paths win here'}], user_id=USER)
    # All episodes share the same signature: rest group empty → waits.
    [verdict] = evolution.validate_hypotheses(user_id=USER)
    assert verdict['status'] == 'proposed'
    row_features = evolution.path_features_for_candidate(
        {'stages': [{'kind': 'index', 'detail': 'd'},
                    {'kind': 'execute', 'detail': 'd'}]},
        {'exploratory': False})
    assert row_features['stage_signature'] == 'index>execute'


def test_author_hypotheses_parses_llm_lines_and_tags_lineage(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success', history_refs=1)

    def fake_author(request):
        assert 'has_history_resource' in request['features']
        assert 'success' in request['digest']
        return [{'feature': 'has_history_resource', 'operator': 'eq',
                 'threshold': True, 'direction': 'helps',
                 'statement': 'from the author'}]

    saved = evolution.author_hypotheses('code', user_id=USER,
                                        author=fake_author)
    assert len(saved) == 1
    assert saved[0]['domain'] == 'code'
    assert saved[0]['lineage'] == ['e1']
    assert evolution.author_hypotheses('empty-domain', user_id=USER,
                                       author=fake_author) == []

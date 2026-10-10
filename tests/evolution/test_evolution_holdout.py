"""Holdout regression and the auto-rollback drill for the evolution gate."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settle(store, goal_id, episode_id, outcome, *, domain='code',
            command='true', settled_at=100):
    from lib import evolution
    from lib.evolution import records
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': goal_id,
        'statement': f'goal in {domain}', 'domain': domain,
        'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': command}}],
        'budget_envelope': {'max_tokens': 100, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 1},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': goal_id,
        'plan_id': 'p', 'selected_path_id': 'path-a', 'exploratory': False,
        'task_id': f't-{episode_id}', 'status': outcome,
        'created_at': 1, 'updated_at': settled_at, 'settled_at': settled_at,
        'actuals': {'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
        'verdict': {'outcome': outcome, 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def _propose(store, kind='prompt_variant', payload=None, evidence='e1', now=200):
    from lib import evolution
    return evolution.propose_mutation(
        kind, payload if payload is not None else {'text': 'be precise'},
        [evidence], user_id=USER, now=now)


def test_holdout_suite_keeps_machine_checkable_settled_goals(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    _settle(store, 'g2', 'e2', 'failure')
    items = evolution.build_holdout_suite(user_id=USER, now=150)
    assert len(items) == 2
    assert {i['baseline_weight'] for i in items} == {1.0, 0.0}
    assert all(i['lineage'] for i in items)


def test_regression_verdict_compares_probe_score_to_baseline(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    evolution.build_holdout_suite(user_id=USER, now=150)
    mutation = _propose(store)
    receipt = evolution.run_regression(
        mutation, user_id=USER,
        probe=lambda m, item: 'pass', now=210)
    assert receipt['verdict'] == 'pass'
    assert receipt['score'] == 1.0
    failing = evolution.run_regression(
        mutation, user_id=USER, probe=lambda m, item: 'fail', now=220)
    assert failing['verdict'] == 'fail'


def test_unmeasurable_kids_need_an_explicit_waiver_to_activate(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    evolution.build_holdout_suite(user_id=USER, now=150)
    mutation = _propose(store, kind='routing_change',
                        payload={'model': 'cheaper'})
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    receipt = evolution.run_regression(mutation, user_id=USER, now=210)
    assert receipt['verdict'] == 'unmeasurable'
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(mutation['mutation_id'], user_id=USER)
    activated = evolution.activate_mutation(
        mutation['mutation_id'], user_id=USER, allow_unmeasurable=True)
    assert activated['status'] == 'active'


def test_failed_regression_blocks_activation(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    evolution.build_holdout_suite(user_id=USER, now=150)
    mutation = _propose(store)
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    evolution.run_regression(mutation, user_id=USER,
                             probe=lambda m, item: 'fail', now=210)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(mutation['mutation_id'], user_id=USER,
                                    allow_unmeasurable=True)


def test_monitoring_rolls_back_a_degrading_mutation_and_restores_parent(store):
    """The P5 drill: post-activation degradation triggers automatic rollback."""
    from lib import evolution
    _settle(store, 'g0', 'e0', 'success', settled_at=50)
    evolution.build_holdout_suite(user_id=USER, now=60)
    parent = _propose(store, payload={'text': 'v1'}, evidence='e0', now=100)
    evolution.confirm_mutation(parent['mutation_id'], user_id=USER, now=101)
    evolution.run_regression(parent, user_id=USER,
                             probe=lambda m, item: 'pass', now=102)
    evolution.activate_mutation(parent['mutation_id'], user_id=USER, now=103)

    child = _propose(store, payload={'text': 'v2'}, evidence='e0', now=110)
    evolution.confirm_mutation(child['mutation_id'], user_id=USER, now=111)
    evolution.run_regression(child, user_id=USER,
                             probe=lambda m, item: 'pass', now=112)
    evolution.activate_mutation(child['mutation_id'], user_id=USER, now=120)

    for index in range(3):
        _settle(store, f'g-post-{index}', f'e-post-{index}', 'failure',
                settled_at=130 + index)
    for index in range(3):
        _settle(store, f'g-independent-{index}', f'e-independent-{index}', 'success', settled_at=80+index)
    evolution.build_holdout_suite(user_id=USER,now=180)
    def paired(mutation,item):
        return {arm:{'outcome':outcome,'tokens':None,'wall_clock_seconds':1}
                for arm,outcome in [('baseline','pass'),('candidate','fail')]}
    evolution.run_regression(child,user_id=USER,paired_probe=paired,now=190)
    report = evolution.monitor_active_mutations(user_id=USER, now=200)
    assert report and report[0]['breach'] is True
    by_id = {m['mutation_id']: m
             for m in evolution.list_mutations(user_id=USER)}
    assert by_id[child['mutation_id']]['status'] == 'rolled_back'
    assert by_id[parent['mutation_id']]['status'] == 'active'


def test_monitoring_tolerates_small_samples_and_healthy_mutations(store):
    from lib import evolution
    _settle(store, 'g0', 'e0', 'success', settled_at=50)
    evolution.build_holdout_suite(user_id=USER, now=60)
    mutation = _propose(store, evidence='e0', now=100)
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER, now=101)
    evolution.run_regression(mutation, user_id=USER,
                             probe=lambda m, item: 'pass', now=102)
    evolution.activate_mutation(mutation['mutation_id'], user_id=USER, now=103)
    _settle(store, 'g-post', 'e-post', 'failure', settled_at=130)
    report = evolution.monitor_active_mutations(user_id=USER, now=200)
    assert report[0]['breach'] is False
    assert evolution.list_mutations(user_id=USER)[0]['status'] == 'active'


@pytest.mark.parametrize('kind,payload', [
    ('prompt_variant', {'text': 'New prompt'}),
    ('macro_tool', {'sequence': ['execute_code']}),
    ('skill_candidate', {'name': 'planner-policy-test'}),
    ('retrieval_policy', {'policy': 'new retrieval'}),
])
def test_default_probe_cannot_certify_a_change_it_does_not_apply(store, kind, payload):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    evolution.build_holdout_suite(user_id=USER)
    mutation = _propose(store, kind=kind, payload=payload)
    receipt = evolution.run_regression(mutation, user_id=USER)
    assert receipt['verdict'] == 'unmeasurable'
    assert all(r['outcome'] == 'skip' for r in receipt['item_results'])


def test_partial_probe_coverage_never_certifies_a_regression_pass(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    _settle(store, 'g2', 'e2', 'failure')
    evolution.build_holdout_suite(user_id=USER)
    mutation = _propose(store)
    receipt = evolution.run_regression(mutation, user_id=USER,
        probe=lambda m, item: 'pass' if item['baseline_weight'] == 1 else 'skip')
    assert receipt['verdict'] == 'unmeasurable'
    assert receipt['baseline'] == 1.0  # identical measured subset, not all historical rows


def test_paired_regression_excludes_training_lineage_and_records_fresh_baseline(store):
    from lib import evolution
    _settle(store, 'training', 'train', 'success')
    for i in range(3):
        _settle(store, f'g{i}', f'e{i}', 'success', settled_at=101+i)
    evolution.build_holdout_suite(user_id=USER)
    mutation = _propose(store, evidence='train')
    observed = []
    def paired(m, item):
        observed.extend(item['lineage'])
        return {arm: {'outcome': outcome, 'tokens': None, 'wall_clock_seconds': 1}
                for arm, outcome in [('baseline', 'fail'), ('candidate', 'pass')]}
    receipt = evolution.run_regression(mutation, user_id=USER, paired_probe=paired)
    assert 'train' not in observed and len(observed) == 3
    assert receipt['baseline'] == 0  # Not the stored historical success rate.
    assert receipt['comparison']['conclusion'] == 'observed_gain'
    assert receipt['comparison']['sign_test_p'] == 0.125
    assert receipt['verdict'] == 'pass'
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    assert evolution.activate_mutation(mutation['mutation_id'], user_id=USER)['status'] == 'active'


def test_regression_refuses_forged_foreign_or_stale_candidate(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    mutation = _propose(store)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.run_regression({**mutation, 'payload': {'text': 'different'}}, user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.run_regression(mutation, user_id=USER+1)


def test_stale_regression_cannot_activate_even_with_waiver(store):
    from lib import evolution
    from lib.evolution import records
    _settle(store, 'g1', 'e1', 'success')
    mutation = _propose(store)
    evolution.run_regression(mutation, user_id=USER)
    confirmed = evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    confirmed['payload'] = {'text': 'changed after measurement'}
    records.put_record(records.NAMESPACE_MUTATIONS, f"{USER}:{mutation['mutation_id']}", confirmed, kind='mutation')
    with pytest.raises(evolution.EvolutionStateError, match='stale'):
        evolution.activate_mutation(mutation['mutation_id'], user_id=USER, allow_unmeasurable=True)


def test_invalid_paired_probe_stays_unmeasurable(store):
    from lib import evolution
    _settle(store, 'train', 'train', 'success')
    _settle(store, 'test', 'test', 'success')
    evolution.build_holdout_suite(user_id=USER)
    mutation = _propose(store, evidence='train')
    receipt = evolution.run_regression(mutation, user_id=USER, paired_probe=lambda *_: {'baseline': 'pass'})
    assert receipt['verdict'] == 'unmeasurable'
    assert receipt['comparison']['measured'] == 0


def test_new_holdout_invalidates_old_activation_receipt(store):
    from lib import evolution
    _settle(store, 'g1', 'e1', 'success')
    evolution.build_holdout_suite(user_id=USER)
    mutation = _propose(store)
    evolution.run_regression(mutation, user_id=USER, probe=lambda *_: 'pass')
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    _settle(store, 'g2', 'e2', 'success')
    evolution.build_holdout_suite(user_id=USER)
    with pytest.raises(evolution.EvolutionStateError, match='suite changed'):
        evolution.activate_mutation(mutation['mutation_id'], user_id=USER)

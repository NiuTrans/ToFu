"""Strategy library: templates need lineage; one failure is never a pattern."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _run(store, goal_id, episode_id, outcome, *, statement, domain='code',
         stage_kind='execute', failed_stage=None, exploratory=False):
    from lib import evolution
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': goal_id, 'statement': statement,
        'domain': domain, 'exploratory': exploratory,
        'criteria': [] if exploratory else [
            {'criterion_id': 'c1', 'kind': 'test_command',
             'params': {'command': 'x'}}],
        'budget_envelope': {'max_tokens': 1000, 'max_wall_clock_seconds': 100,
                            'max_tool_calls': 10},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    evolution.save_plan({
        'schema_version': 1, 'plan_id': f'plan-{goal_id}', 'goal_id': goal_id,
        'plan_revision': 1,
        'candidates': [{
            'path_id': 'path-a', 'title': 'P',
            'stages': [{'kind': stage_kind, 'detail': 'd'}],
            'resource_refs': [],
            'cost_estimate': {
                'expected_tokens': 10, 'expected_wall_clock_seconds': 10,
                'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                'expected_information_gain': 0.5, 'expected_utility': 0.5,
                'priors_source': 'exploratory_flat'},
            'utility_over_cost': 1.0}],
        'selected_path_id': 'path-a', 'selection_rationale': 'r',
        'requires_confirmation': True, 'status': 'confirmed',
        'created_at': 1}, user_id=USER)
    episode = evolution.open_episode(goal_id=goal_id, plan_id=f'plan-{goal_id}',
                                     selected_path_id='path-a',
                                     task_id=f't-{episode_id}', user_id=USER)
    receipts = ([{'stage': failed_stage, 'status': 'error'}]
                if failed_stage else [])
    verdict = None if outcome == 'aborted' else {
        'outcome': outcome, 'assessed_by': 'checker', 'criterion_results': []}
    evolution.settle_episode(
        episode['episode_id'], user_id=USER, status=outcome,
        actuals={'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
        stage_receipts=receipts, verdict=verdict,
        learning={'resource_weight_deltas': [], 'cost_actual_vs_estimate': None,
                  'strategy_outcome': None, 'capability_gap': None})
    return episode['episode_id']


def test_distill_groups_successes_into_retrievable_templates(store):
    from lib import evolution
    first = _run(store, 'g1', 'e1', 'success',
                 statement='reproduce transformer baseline')
    second = _run(store, 'g2', 'e2', 'partial',
                  statement='reproduce transformer results')
    out = evolution.distill_strategies(user_id=USER, now=1700000005)
    assert len(out['templates']) == 1
    template = out['templates'][0]
    assert template['samples'] == 2
    assert template['success_rate'] == 0.75
    assert set(template['lineage']) == {first, second}
    hits = evolution.retrieve_strategies('reproduce transformer', user_id=USER)
    assert [h['template_id'] for h in hits] == [template['template_id']]
    assert evolution.retrieve_strategies('unrelated zzq topic', user_id=USER) == []


def test_anti_pattern_needs_two_failures_with_the_same_signature(store):
    from lib import evolution
    _run(store, 'g1', 'e1', 'failure',
         statement='train the model', failed_stage='train')
    out = evolution.distill_strategies(user_id=USER, now=1700000005)
    assert out['anti_patterns'] == []  # one incident is not a pattern
    _run(store, 'g2', 'e2', 'failure',
         statement='train the model again', failed_stage='train')
    out = evolution.distill_strategies(user_id=USER, now=1700000006)
    assert len(out['anti_patterns']) == 1
    pattern = out['anti_patterns'][0]
    assert pattern['samples'] == 2
    assert pattern['signature'] == 'failed:train'
    assert evolution.list_anti_patterns(user_id=USER, domain='code') == [pattern]


def test_exploratory_episodes_contribute_no_strategies(store):
    from lib import evolution
    _run(store, 'g1', 'e1', 'success', statement='vague exploration',
         exploratory=True)
    out = evolution.distill_strategies(user_id=USER, now=1700000005)
    assert out == {'templates': [], 'anti_patterns': []}


def test_strategy_rate_includes_failures_of_the_same_path(store):
    from lib import evolution
    ids = [_run(store, 'g0', 'e0', 'success', statement='repair parser')]
    for index in range(1, 4):
        ids.append(_run(store, f'g{index}', f'e{index}', 'failure',
                        statement='repair parser', failed_stage='execute'))
    template=evolution.distill_strategies(user_id=USER)['templates'][0]
    assert template['samples']==4
    assert template['success_rate']==0.25
    assert set(template['lineage'])==set(ids)


@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_learning_window_never_exceeds_its_receipt_contract(store, outcome):
    from lib import evolution
    for index in range(65):
        _run(store,f'g{index}',f'e{index}',outcome,
             statement='repair parser',failed_stage='execute' if outcome=='failure' else None)
    result=evolution.distill_strategies(user_id=USER)
    record=(result['templates'] if outcome=='success' else result['anti_patterns'])[0]
    assert record['samples']==len(record['lineage'])==64


@pytest.mark.parametrize('receipts', [[], [{'stage':'train','status':'done'}],
    [{'stage':'train','status':'pending'}], [{'stage':'train','status':'unknown'}],
    [{'stage':'','status':'error'}], [{'stage':'  ','status':'failed'}]])
def test_failure_without_confirmed_stage_does_not_mint_avoidance_advice(store, receipts):
    from copy import deepcopy
    from lib import evolution
    from lib.evolution.records import NAMESPACE_EPISODES
    ids = [_run(store, f'g{index}', f'e{index}', 'failure', statement='train model')
        for index in range(2)]
    for episode_id in ids:
        store.rows[NAMESPACE_EPISODES, f'{USER}:{episode_id}']['stage_receipts'] = deepcopy(receipts)
    result = evolution.distill_strategies(user_id=USER)
    assert result['anti_patterns'] == []
    assert evolution.list_anti_patterns(user_id=USER) == []


@pytest.mark.parametrize('status', ['error','failed','failure','fail'])
def test_only_explicit_failed_stage_contributes_even_after_pending_and_done_receipts(store, status):
    from lib import evolution
    from lib.evolution.records import NAMESPACE_EPISODES
    ids = [_run(store, f'g{index}', f'e{index}', 'failure', statement='train model')
        for index in range(2)]
    for episode_id in ids:
        store.rows[NAMESPACE_EPISODES, f'{USER}:{episode_id}']['stage_receipts'] = [
            {'stage':'prepare','status':'pending'}, {'stage':'load','status':'done'},
            {'stage':'train','status':status}]
    pattern = evolution.distill_strategies(user_id=USER)['anti_patterns'][0]
    assert pattern['signature'] == 'failed:train'
    assert pattern['failure_evidence_version'] == 1
    assert set(pattern['lineage']) == set(ids)


def test_legacy_avoidance_is_hidden_until_original_receipts_rebuild_it(store):
    from lib import evolution
    from lib.evolution.records import NAMESPACE_ANTI_PATTERNS
    for index in range(2):
        _run(store, f'g{index}', f'e{index}', 'failure', statement='train model', failed_stage='train')
    pattern = evolution.distill_strategies(user_id=USER)['anti_patterns'][0]
    stored = store.rows[NAMESPACE_ANTI_PATTERNS, f"{USER}:{pattern['pattern_id']}"]
    stored.pop('failure_evidence_version')
    assert evolution.list_anti_patterns(user_id=USER) == []
    assert evolution.list_anti_patterns(user_id=99) == []
    assert len(store.rows) > 0  # Historical evidence is retained, not deleted.
    rebuilt = evolution.distill_strategies(user_id=USER)['anti_patterns'][0]
    assert evolution.list_anti_patterns(user_id=USER) == [rebuilt]


def test_legacy_records_do_not_fill_result_cap_before_verified_advice(store):
    from copy import deepcopy
    from lib import evolution
    from lib.evolution import records
    for index in range(2):
        _run(store, f'g{index}', f'e{index}', 'failure', statement='train model', failed_stage='train')
    pattern = evolution.distill_strategies(user_id=USER)['anti_patterns'][0]
    store.rows.pop((records.NAMESPACE_ANTI_PATTERNS, f"{USER}:{pattern['pattern_id']}"))
    for index in range(65):
        legacy = deepcopy(pattern)
        legacy.pop('failure_evidence_version')
        legacy['pattern_id'] = f'anti-old-{index}'
        records.put_record(records.NAMESPACE_ANTI_PATTERNS, f"{USER}:{legacy['pattern_id']}", legacy, kind='anti-pattern')
    pattern['pattern_id'] = 'z-verified'
    records.put_record(records.NAMESPACE_ANTI_PATTERNS, f'{USER}:z-verified', pattern, kind='anti-pattern')
    assert evolution.list_anti_patterns(user_id=USER, domain='code') == [pattern]

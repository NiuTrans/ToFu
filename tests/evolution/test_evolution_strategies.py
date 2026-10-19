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
         stage_kind='execute', failed_stage=None, exploratory=False, stage_rows=None):
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
            'stages': stage_rows if stage_rows is not None else [{'kind': stage_kind, 'detail': 'd'}],
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
def test_learning_preserves_every_lineage_receipt(store, outcome):
    from lib import evolution
    for index in range(65):
        _run(store,f'g{index}',f'e{index}',outcome,
             statement='repair parser',failed_stage='execute' if outcome=='failure' else None)
    result=evolution.distill_strategies(user_id=USER)
    record=(result['templates'] if outcome=='success' else result['anti_patterns'])[0]
    assert record['samples']==len(record['lineage'])==65


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


def test_complete_distillation_and_retrieval_survive_all_old_count_limits(store):
    from lib import evolution
    terms = ' '.join(f'keyword{index:02d}' for index in range(48))
    for index in range(241):
        domain = f'domain{index:03d}'
        for suffix, outcome in [('s', 'success'), ('a', 'failure'), ('b', 'failure')]:
            _run(store, f'goal-{index}-{suffix}', f'episode-{index}-{suffix}', outcome,
                 statement=terms, domain=domain, failed_stage='execute')
    import time
    import tracemalloc
    import json
    tracemalloc.start()
    started = time.perf_counter()
    result = evolution.distill_strategies(user_id=USER)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    bytes_written = sum(len(json.dumps(row, ensure_ascii=False).encode())
                        for group in result.values() for row in group)
    print(f'strategy_episodes=723 templates=241 patterns=241 json_bytes={bytes_written} '
          f'distill_seconds={elapsed:.4f} traced_peak_bytes={peak}')
    assert len(result['templates']) == len(result['anti_patterns']) == 241
    assert len(evolution.list_strategies(user_id=USER)) == 241
    assert len(evolution.list_anti_patterns(user_id=USER)) == 241
    assert len(evolution.retrieve_strategies('keyword47', user_id=USER, limit=241)) == 241
    assert len(evolution.retrieve_strategies('keyword47', user_id=USER,
                                           domain='domain240', limit=241)) == 1
    assert evolution.list_strategies(user_id=USER + 1) == []
    for template in result['templates']:
        assert template['samples'] == len(template['lineage']) == 3
        assert len(template['applicability_terms']) == 48
        assert template['success_rate'] == 0.3333


def test_long_applicability_term_remains_retrievable(store):
    from lib import evolution
    term = 'longtoken' * 20
    _run(store, 'long-goal', 'long-episode', 'success', statement=term)
    template = evolution.distill_strategies(user_id=USER)['templates'][0]
    assert template['applicability_terms'] == [term]
    assert evolution.retrieve_strategies(term, user_id=USER) == [template]


def test_large_lineage_record_reaches_storage_without_application_byte_quota(store):
    from lib.evolution.contracts import validate_strategy
    from lib.evolution.records import NAMESPACE_STRATEGIES, put_record, get_record, entity_key
    import json
    lineage = [f'episode-{index:06d}-' + 'x' * 120 for index in range(9000)]
    record = validate_strategy({'schema_version': 1, 'template_id': 'large-template',
        'domain': 'code', 'stages': [{'kind': 'execute', 'detail': 'd'}],
        'applicability_terms': ['needle'], 'lineage': lineage, 'samples': len(lineage),
        'success_rate': 1, 'created_at': 1, 'updated_at': 1})
    assert len(json.dumps(record).encode()) > 1 << 20
    key = entity_key(record['template_id'], USER, kind='strategy')
    put_record(NAMESPACE_STRATEGIES, key, record, kind='strategy')
    assert get_record(NAMESPACE_STRATEGIES, key) == record


def test_distillation_reuses_procedure_without_historical_goal_target(store):
    from copy import deepcopy
    from lib import evolution
    from lib.evolution.planner import baseline_stages
    from lib.evolution.goals import get_plan
    goal = 'repair legacy planner at /private/old-target using former inputs'
    stages = baseline_stages()
    stages[0]['detail'] = 'Inspect the current context and constraints for this objective: ' + goal
    original = deepcopy(stages)
    episode_id = _run(store, 'old', 'old-episode', 'success', statement=goal, stage_rows=stages)
    template = evolution.distill_strategies(user_id=USER)['templates'][0]
    assert template['stages'] == baseline_stages()
    assert template['lineage'] == [episode_id] and template['success_rate'] == 1.0
    assert get_plan('plan-old', user_id=USER)['candidates'][0]['stages'] == original
    assert stages == original

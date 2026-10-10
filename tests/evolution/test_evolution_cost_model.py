"""Measured cost priors: episodes calibrate estimates, unknown is never zero."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _admit(domain, goal_id, *, exploratory=False):
    from lib import evolution
    criteria = [] if exploratory else [
        {'criterion_id': 'c1', 'kind': 'test_command', 'params': {'command': 'x'}}]
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': goal_id,
        'statement': f'goal in {domain}', 'domain': domain,
        'exploratory': exploratory, 'criteria': criteria,
        'budget_envelope': {'max_tokens': 100000, 'max_wall_clock_seconds': 3600,
                            'max_tool_calls': 100},
        'status': 'draft', 'created_at': 1700000000}, user_id=USER)


def _settle(domain, goal_id, episode_id, *, tokens, wall, calls, outcome,
            user_id=USER):
    from lib.evolution import records
    verdict = None if outcome == 'aborted' else {
        'outcome': outcome, 'assessed_by': 'checker', 'criterion_results': []}
    records.put_record(records.NAMESPACE_EPISODES, f'{user_id}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': goal_id,
        'plan_id': 'p', 'selected_path_id': 'path-a',
        'exploratory': outcome == 'aborted', 'task_id': f't-{episode_id}',
        'status': outcome, 'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': tokens, 'wall_clock_seconds': wall,
                    'tool_calls': calls},
        'verdict': verdict,
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def test_empty_history_is_exploratory_flat_with_envelope_values(store):
    from lib import evolution
    estimate = evolution.estimate_cost(
        'code', user_id=USER,
        budget_envelope={'max_tokens': 80000, 'max_wall_clock_seconds': 900,
                         'max_tool_calls': 40})
    assert estimate == {'expected_tokens': 80000,
                        'expected_wall_clock_seconds': 900,
                        'expected_tool_calls': 40,
                        'failure_rate_prior': 0.5,
                        'priors_source': 'exploratory_flat'}


def test_medians_and_failure_rate_come_from_settled_episodes(store):
    from lib import evolution
    _admit('code', 'g1')
    _settle('code', 'g1', 'e1', tokens=100, wall=10, calls=1, outcome='success')
    _settle('code', 'g1', 'e2', tokens=300, wall=30, calls=3, outcome='failure')
    _settle('code', 'g1', 'e3', tokens=200, wall=20, calls=2, outcome='success')
    _settle('code', 'g1', 'e4', tokens=99999, wall=9999, calls=99,
            outcome='aborted')  # censored: feeds nothing
    records = evolution.recompute_priors(user_id=USER, now=1700000005)
    by_domain = {r['domain']: r for r in records}
    assert by_domain['code']['samples'] == 3
    assert by_domain['code']['tokens_median'] == 200
    assert by_domain['code']['failure_rate'] == pytest.approx(1 / 3)
    assert by_domain['_global']['samples'] == 3
    estimate = evolution.estimate_cost('code', user_id=USER)
    assert estimate['priors_source'] == 'measured_history'
    assert estimate['expected_tokens'] == 200
    assert estimate['failure_rate_prior'] == pytest.approx(1 / 3)


def test_sparse_domain_falls_back_to_global_default(store):
    from lib import evolution
    _admit('code', 'g1')
    _admit('research', 'g2')
    for index in range(3):
        _settle('code', 'g1', f'e{index}', tokens=100 * (index + 1), wall=10,
                calls=1, outcome='success')
    _settle('research', 'g2', 'e9', tokens=5000, wall=60, calls=9,
            outcome='success')
    evolution.recompute_priors(user_id=USER, now=1700000005)
    estimate = evolution.estimate_cost('research', user_id=USER)
    assert estimate['priors_source'] == 'domain_default'
    assert estimate['expected_tokens'] == 250  # global median of all 4, not zero
    code = evolution.estimate_cost('code', user_id=USER)
    assert code['priors_source'] == 'measured_history'
    assert code['expected_tokens'] == 200


def test_priors_are_owner_scoped(store):
    from lib import evolution
    _admit('code', 'g1')
    _settle('code', 'g1', 'e1', tokens=100, wall=10, calls=1, outcome='success')
    evolution.recompute_priors(user_id=USER, now=1700000005)
    assert evolution.list_priors(user_id=99) == []
    assert evolution.estimate_cost('code', user_id=99)[
        'priors_source'] == 'exploratory_flat'


def test_calibration_converges_toward_actuals_over_episodes(store):
    """The P1 budget test: estimation error shrinks as episodes accumulate."""
    from lib import evolution
    _admit('code', 'g1')
    actuals = [120, 130, 110, 125, 115, 122, 118]
    errors = []
    for index, tokens in enumerate(actuals):
        _settle('code', 'g1', f'e{index}', tokens=tokens, wall=10, calls=1,
                outcome='success')
        evolution.recompute_priors(user_id=USER, now=1700000005 + index)
        estimate = evolution.estimate_cost('code', user_id=USER)
        if estimate['priors_source'] == 'measured_history':
            errors.append(abs(estimate['expected_tokens'] - tokens) / tokens)
    assert len(errors) >= 3
    assert errors[-1] <= errors[0]
    assert errors[-1] < 0.25

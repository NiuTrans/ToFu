"""Execution bridge: terminal tasks settle episodes from receipts, not prose."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _goal_and_plan(store):
    from lib import evolution
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': 'goal-1',
        'statement': 'Ship a passing implementation.', 'domain': 'code',
        'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': 'pytest -q'}}],
        'budget_envelope': {'max_tokens': 100000, 'max_wall_clock_seconds': 3600,
                            'max_tool_calls': 100},
        'status': 'draft', 'created_at': 1700000000}, user_id=USER)
    evolution.save_plan({
        'schema_version': 1, 'plan_id': 'plan-1', 'goal_id': 'goal-1',
        'plan_revision': 1,
        'candidates': [{
            'path_id': 'path-a', 'title': 'Adapt reference',
            'stages': [{'kind': 'execute', 'detail': 'port then test'}],
            'resource_refs': [],
            'cost_estimate': {
                'expected_tokens': 50000, 'expected_wall_clock_seconds': 1800,
                'expected_tool_calls': 60, 'failure_rate_prior': 0.3,
                'expected_information_gain': 0.7, 'expected_utility': 0.6,
                'priors_source': 'measured_history'},
            'utility_over_cost': 1.4}],
        'selected_path_id': 'path-a', 'selection_rationale': 'best ratio',
        'requires_confirmation': True, 'status': 'confirmed',
        'created_at': 1700000001}, user_id=USER)


def _task(status='done', **overrides):
    task = {
        'id': 'task-1',
        '_userId': USER,
        'status': status,
        'created_at': 1700000002.0,
        'finished_at': 1700001602.0,
        'usage': {'prompt_tokens': 30000, 'completion_tokens': 17000},
        'toolRounds': [
            {'tool_calls': [{'name': 'a'}, {'name': 'b'}]},
            {'tool_calls': [{'name': 'c'}]},
            {'no_calls': True},
        ],
    }
    task.update(overrides)
    return task


def _verdict(outcome='success'):
    return {'outcome': outcome, 'assessed_by': 'checker',
            'criterion_results': [{'criterion_id': 'c1', 'outcome': 'pass'}]}


def test_derive_actuals_uses_canonical_usage_aliasing_and_task_clocks(store):
    from lib.evolution import bridge
    actuals = bridge.derive_actuals(_task())
    assert actuals == {'tokens': 47000, 'wall_clock_seconds': 1600, 'tool_calls': 3}
    anthropic = _task(usage={'input_tokens': 100, 'output_tokens': 40},
                      toolRounds=[])
    assert bridge.derive_actuals(anthropic)['tokens'] == 140
    no_clock = _task(finished_at=None)
    assert bridge.derive_actuals(no_clock)['wall_clock_seconds'] == 0


def test_running_task_is_not_settleable(store):
    from lib import evolution
    from lib.evolution import bridge
    _goal_and_plan(store)
    episode = evolution.open_episode(goal_id='goal-1', plan_id='plan-1',
                                     selected_path_id='path-a', task_id='task-1',
                                     user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        bridge.settle_from_task(episode['episode_id'], _task(status='running'),
                                user_id=USER, verdict=_verdict())


def test_done_task_still_requires_a_criteria_verdict(store):
    from lib import evolution
    from lib.evolution import bridge
    _goal_and_plan(store)
    episode = evolution.open_episode(goal_id='goal-1', plan_id='plan-1',
                                     selected_path_id='path-a', task_id='task-1',
                                     user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        bridge.settle_from_task(episode['episode_id'], _task(), user_id=USER)
    assert evolution.get_episode(episode['episode_id'], user_id=USER)['status'] == 'running'


def test_aborted_task_settles_aborted_without_verdict(store):
    from lib.evolution import bridge
    _goal_and_plan(store)
    settled = bridge.record_terminal_task(
        _task(status='aborted', usage={}), goal_id='goal-1', plan_id='plan-1',
        selected_path_id='path-a', user_id=USER)
    assert settled['status'] == 'aborted'
    assert settled['verdict'] is None


def test_terminal_task_roundtrip_carries_receipt_actuals(store):
    from lib.evolution import bridge
    _goal_and_plan(store)
    settled = bridge.record_terminal_task(
        _task(), goal_id='goal-1', plan_id='plan-1', selected_path_id='path-a',
        user_id=USER, verdict=_verdict(),
        learning={'resource_weight_deltas': [],
                  'cost_actual_vs_estimate': {'tokens_ratio': 0.94,
                                              'wall_clock_ratio': 0.89},
                  'strategy_outcome': None, 'capability_gap': None})
    assert settled['status'] == 'success'
    assert settled['actuals'] == {'tokens': 47000, 'wall_clock_seconds': 1600,
                                  'tool_calls': 3}
    assert settled['task_id'] == 'task-1'


def test_foreign_task_is_rejected_by_loader_and_dict_check(store):
    from lib import evolution
    from lib.evolution import bridge
    _goal_and_plan(store)
    with pytest.raises(evolution.EvolutionStateError):
        bridge.open_for_task(_task(_userId=99), goal_id='goal-1', plan_id='plan-1',
                             selected_path_id='path-a', user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        bridge.open_for_task('task-missing', goal_id='goal-1', plan_id='plan-1',
                             selected_path_id='path-a', user_id=USER,
                             loader=lambda task_id, *, user_id: None)


@pytest.mark.parametrize('complete', [False, True])
def test_bound_research_terminal_uses_physical_account_lower_bound(monkeypatch, complete):
    from lib.evolution import bridge
    from lib.research import budget_binding, model_attempt_journal
    calls = []
    monkeypatch.setattr(budget_binding, 'read_model_budget', lambda task_id, **kwargs: {'bound': True})
    def summary(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return {'account_token_lower_bound': 90000, 'recorded_usage_complete': complete}
    monkeypatch.setattr(model_attempt_journal, 'summarize_model_attempts', summary)
    actuals, observed_complete = bridge.terminal_actuals(_task(kind='research'))
    assert actuals == {'tokens': 90000, 'wall_clock_seconds': 1600, 'tool_calls': 3}
    assert observed_complete is complete
    assert calls == [('task-1', {'user_id': USER})]


def test_unbound_research_terminal_retains_legacy_usage(monkeypatch):
    from lib.evolution import bridge
    from lib.research import budget_binding, model_attempt_journal
    monkeypatch.setattr(budget_binding, 'read_model_budget', lambda *a, **k: None)
    monkeypatch.setattr(model_attempt_journal, 'summarize_model_attempts', lambda *a, **k: pytest.fail('unbound journal'))
    actuals, complete = bridge.terminal_actuals(_task(kind='research'))
    assert actuals['tokens'] == 47000 and complete


def test_research_account_read_failure_cannot_become_zero_cost(monkeypatch):
    from lib.evolution import bridge
    from lib.research import budget_binding
    def unavailable(*args, **kwargs):
        raise RuntimeError('account unavailable')
    monkeypatch.setattr(budget_binding, 'read_model_budget', unavailable)
    with pytest.raises(RuntimeError, match='account unavailable'):
        bridge.terminal_actuals(_task(kind='research'))

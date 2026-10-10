"""Aggregate child budgets: bounded parents delegate only their remaining
execution envelope to swarm children, and completed child bills fold back
into the parent's admission counters and evolution episode actuals.
"""
import json
import types

import pytest

pytestmark = pytest.mark.unit

from lib.agent_core.execution_limits import execution_limit_reason
from lib.evolution.bridge import derive_actuals
from lib.swarm.integration._tools import (
    _child_usage_rollup, _handle_spawn_agents, plan_child_execution_limits)


def _bounded_task(**overrides):
    task = {
        'id': 'parent-task',
        'config': {'executionLimits': {
            'max_tokens': 10_000, 'max_tool_calls': 20, 'deadline': 4_000_000_000}},
        'usage': {'prompt_tokens': 1_000, 'completion_tokens': 500},
    }
    task.update(overrides)
    return task


class TestPlanChildExecutionLimits:
    def test_unbounded_parent_keeps_unbounded_children(self):
        assert plan_child_execution_limits({'config': {}}, 3) == (None, None)

    def test_wave_slices_remaining_budget(self):
        limits, error = plan_child_execution_limits(_bounded_task(), 3)
        assert error is None
        assert limits == {'max_tokens': (10_000 - 1_500) // 3,
                          'max_tool_calls': 20 // 3,
                          'deadline': 4_000_000_000}

    def test_prior_waves_shrink_later_slices(self):
        task = _bounded_task(
            _swarmChildUsage={'prompt_tokens': 6_000},
            _executionToolsAdmitted=18)
        limits, error = plan_child_execution_limits(task, 2)
        assert error is None
        assert limits['max_tokens'] == (10_000 - 1_500 - 6_000) // 2
        assert limits['max_tool_calls'] == (20 - 18) // 2

    def test_exhausted_budget_rejects_the_wave(self):
        tokens, calls = plan_child_execution_limits(
            _bounded_task(usage={'prompt_tokens': 10_000}), 2)[1]
        assert tokens == 0
        assert calls == 20
        _, error = plan_child_execution_limits(
            _bounded_task(_executionToolsAdmitted=20), 2)
        assert error == (8_500, 0)

    @pytest.mark.parametrize('malformed', [
        {}, {'max_tokens': 'soon'}, False, 'unbounded',
        {'max_tokens': True, 'max_tool_calls': None, 'deadline': None},
        {'max_tokens': None, 'max_tool_calls': -1, 'deadline': None},
        {'max_tokens': None, 'max_tool_calls': None, 'deadline': float('nan')},
        {'max_tokens': None, 'max_tool_calls': None, 'deadline': float('inf')},
    ])
    def test_malformed_limits_reject_delegation(self, malformed):
        task = _bounded_task()
        task['config']['executionLimits'] = malformed
        assert plan_child_execution_limits(task, 2) == (None, (0, 0))


class TestChildUsageRollup:
    def test_unbounded_parent_gets_no_rollup(self):
        assert _child_usage_rollup({'config': {}}) is None

    def test_rollup_folds_bill_into_admission_counters(self):
        task = _bounded_task(_executionToolsAdmitted=1)
        rollup = _child_usage_rollup(task)
        rollup({'prompt_tokens': 100, 'completion_tokens': 50,
                'total_tokens': 150, 'cost_usd': 0.01}, 3)
        rollup({'prompt_tokens': 25, 'total_tokens': 25}, 2)
        assert task['_swarmChildUsage']['prompt_tokens'] == 125
        assert task['_swarmChildUsage']['completion_tokens'] == 50
        assert task['_executionToolsAdmitted'] == 6
        assert task['_swarmChildToolCalls'] == 5

    def test_master_completion_invokes_rollup(self):
        from lib.swarm.master import MasterOrchestrator
        from lib.swarm.types import SubAgentResult, SubTaskSpec
        seen = []
        master = MasterOrchestrator(
            'task', 'conv', [], user_id=1,
            on_agent_usage=lambda usage, calls: seen.append((usage, calls)))
        master._publish_completion_inbox = lambda spec, result: None
        spec = SubTaskSpec(objective='x')
        result = SubAgentResult(
            status='completed', prompt_tokens=10, completion_tokens=5,
            total_tokens=15, tool_calls_made=4)
        master._on_agent_complete_callback(spec, result)
        assert seen == [({'prompt_tokens': 10, 'completion_tokens': 5,
                          'total_tokens': 15, 'cost_usd': 0.0}, 4)]


class TestBudgetEnforcementSeesChildUsage:
    def test_parent_admission_counts_folded_child_tokens(self):
        task = _bounded_task(usage={'prompt_tokens': 9_000},
                             _swarmChildUsage={'prompt_tokens': 2_000})
        assert execution_limit_reason(task) == 'execution_token_budget_exhausted'

    def test_episode_actuals_include_child_consumption(self):
        task = _bounded_task(
            created_at=100, finished_at=160,
            usage={'prompt_tokens': 1_000},
            _swarmChildUsage={'prompt_tokens': 300, 'completion_tokens': 100},
            _swarmChildToolCalls=7)
        actuals = derive_actuals(task)
        assert actuals['tokens'] == 1_400
        assert actuals['tool_calls'] == 7
        assert actuals['wall_clock_seconds'] == 60


class TestSpawnHandlerBudgetGate:
    def test_exhausted_parent_is_rejected_before_session_work(self):
        task = _bounded_task(usage={'prompt_tokens': 10_000})
        reply = json.loads(_handle_spawn_agents(
            {'agents': [{'objective': 'scout'}]},
            task_id=task['id'], task=task, cfg={}, all_tools=[], model='m',
            thinking_enabled=None, project_path='', abort_check=None,
            on_event=None))
        assert reply['status'] == 'error'
        assert reply['error']['code'] == 'execution_budget_exhausted'
        assert reply['error']['retryable'] is False
        # The context fields the model needs to reason about the rejection
        # must survive to the flat payload (not be swallowed by a wrapper).
        assert 'remaining_tokens' in reply
        assert 'remaining_tool_calls' in reply


class TestChildReceivesItsSlice:
    def test_sub_agent_injects_spec_limits_into_child_config(self, monkeypatch):
        from lib.swarm.agent import SubAgent
        from lib.swarm.types import SubTaskSpec
        captured = {}

        def fake_create(parent, **kwargs):
            return {'id': 'child-1', 'config': {}, 'messages': [],
                    'usage': {'prompt_tokens': 3}}
        monkeypatch.setattr(
            'lib.tasks_pkg.child_invocation.create_child_invocation', fake_create)
        monkeypatch.setattr(
            'lib.tasks_pkg.child_invocation.run_child_invocation',
            lambda child: captured.setdefault('child', child))
        monkeypatch.setattr(
            'lib.turn_verdict.derive_task_verdict',
            lambda child: types.SimpleNamespace(
                status=types.SimpleNamespace(value='completed')))

        spec = SubTaskSpec(
            objective='x', model_override='m',
            execution_limits={'max_tokens': 5, 'max_tool_calls': 1,
                              'deadline': 4_000_000_000})
        agent = SubAgent(spec, parent_task={'id': 'p', 'config': {}},
                         all_tools=[], model='m')
        result = agent.run()
        assert result.status == 'completed'
        assert captured['child']['config']['executionLimits'] == {
            'max_tokens': 5, 'max_tool_calls': 1, 'deadline': 4_000_000_000}


def test_evolution_executor_catalog_no_longer_excludes_swarm():
    from lib.evolution.runs import _task_config
    run = {'run_id': 'r', 'goal_id': 'g', 'plan_id': 'p', 'created_at': 0,
           'budget': {'max_tokens': 10_000, 'max_tool_calls': 10,
                      'max_wall_clock_seconds': 600},
           'actuals': {'tokens': 0, 'tool_calls': 0}, 'role_models': {}}
    executing = _task_config(run, 'executing')
    assert 'toolSpecDenylist' not in executing
    assert 'toolSpecAllowlist' not in executing
    reviewing = _task_config({**run, 'budget': dict(run['budget'])}, 'reviewing')
    assert reviewing['toolSpecAllowlist'] == []

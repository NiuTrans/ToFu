"""Global agent-budget contract across concurrent and nested execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib.orchestration_budget import OrchestrationAgentBudget
from lib.orchestration_engine import FlowExecutor


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_budget_claim_is_atomic_under_contention():
    budget = OrchestrationAgentBudget(7)
    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(pool.map(lambda _index: budget.claim(), range(100)))

    assert claims.count(True) == 7
    assert claims.count(False) == 93
    assert budget.used() == 7
    assert budget.remaining() == 0


def _two_role_child(name):
    return {
        'schema': 'tofu.orchestration/v1',
        'name': name,
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'},
            {'id': 'a', 'type': 'role', 'role': 'worker'},
            {'id': 'b', 'type': 'role', 'role': 'worker'},
            {'id': 'e', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 's', 'to': 'a'},
            {'from': 'a', 'to': 'b'},
            {'from': 'b', 'to': 'e'},
        ],
    }


def test_sequential_isolated_subflows_share_one_global_budget():
    definition = {
        'schema': 'tofu.orchestration/v1',
        'name': 'Budgeted parent',
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'},
            {'id': 'box1', 'type': 'subflow', 'role': 'general',
             'params': {'scope': 'isolated',
                        'definition': _two_role_child('Child one')}},
            {'id': 'box2', 'type': 'subflow', 'role': 'general',
             'params': {'scope': 'isolated',
                        'definition': _two_role_child('Child two')}},
            {'id': 'e', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 's', 'to': 'box1'},
            {'from': 'box1', 'to': 'box2'},
            {'from': 'box2', 'to': 'e'},
        ],
    }

    result = FlowExecutor(
        definition,
        agent_runner=lambda node, _context, _iteration: {
            'output': node['id'], 'status': 'completed', 'error': '',
        },
        max_agents=3,
    ).run()

    assert result['status'] == 'failed'
    assert result['stop_reason'] == 'structural'
    assert result['agents_run'] == 3
    assert 'agent budget exhausted (3)' in result['error']


def test_engine_shares_budget_object_with_nested_executors():
    engine = (ROOT / 'lib/orchestration_engine.py').read_text()
    runtime = (ROOT / 'lib/orchestration_subflow_runtime.py').read_text()
    assert 'OrchestrationAgentBudget' in engine
    assert '_agent_budget=self._agent_budget' in engine
    assert 'child_executor.agents_run' in runtime

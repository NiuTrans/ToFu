"""Contracts for the execution-free orchestration plan compiler."""

from pathlib import Path

import pytest

from lib.orchestration_engine import compile_plan as engine_compile_plan
from lib.orchestration_plan import compile_plan

pytestmark = pytest.mark.unit


def test_engine_reexports_the_single_plan_compiler():
    assert engine_compile_plan is compile_plan


def test_linear_preview_uses_the_shared_topology_order():
    definition = {
        'schema': 'tofu.orchestration/v1',
        'name': 'Linear preview',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start', 'params': {}},
            {
                'id': 'writer',
                'type': 'role',
                'role': 'writer',
                'params': {'objective': 'Write the result.'},
            },
            {'id': 'stop', 'type': 'control', 'kind': 'stop', 'params': {}},
        ],
        'edges': [
            {'from': 'start', 'to': 'writer'},
            {'from': 'writer', 'to': 'stop'},
        ],
    }

    assert compile_plan(definition) == {
        'ok': True,
        'steps': [
            {'node_id': 'start', 'kind': 'start', 'action': 'start'},
            {'node_id': 'writer', 'role': 'writer', 'action': 'run-agent'},
            {'node_id': 'stop', 'kind': 'stop', 'action': 'stop'},
        ],
        'error': None,
    }


def test_plan_compiler_is_not_implemented_by_executor_or_inspection_boundary():
    root = Path(__file__).resolve().parents[1]
    engine = (root / 'lib' / 'orchestration_engine.py').read_text()
    plan = (root / 'lib' / 'orchestration_plan.py').read_text()
    definition_service = (
        root / 'lib' / 'orchestration' / 'definition_service.py'
    ).read_text()
    definition_inspection = (
        root / 'lib' / 'orchestration' / 'definition_inspection.py'
    ).read_text()

    assert 'def compile_plan(' not in engine
    assert 'from lib.orchestration_plan import compile_plan' in engine
    assert 'def compile_plan(' not in definition_service
    assert 'from lib.orchestration_plan import compile_plan' \
        not in definition_service
    assert 'def compile_plan(' not in definition_inspection
    assert 'from lib.orchestration_plan import compile_plan' \
        in definition_inspection
    assert 'GraphNavigator.from_edges(' in plan
    assert "resolve_node_runtime_param(node, 'scope')" in plan
    assert "'scope': 'isolated'" not in plan
    assert "resolve_node_runtime_param(node, 'mode')" in plan
    assert "mode') or 'approve'" not in plan
    assert 'FlowExecutor' not in plan
    assert engine.count('\n') < 1600

"""Contracts for the typed orchestration Agent Runner result port."""

from pathlib import Path

import pytest

from lib.orchestration_runner_result import (
    OrchestrationAgentResult,
    normalize_orchestration_agent_result,
)
from lib.orchestration_tool_usage import OrchestrationToolUsage
from lib.orchestration_engine import FlowExecutor


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_legacy_mapping_normalizes_every_result_axis_once():
    result = normalize_orchestration_agent_result({
        'output': 42,
        'status': 'failed',
        'error': 'provider unavailable',
        'thinking': 'reasoning',
        'tool_log': [{'tool': 'write_file'}, {'tool': 'read_file'}],
    })

    assert result == OrchestrationAgentResult(
        output='42',
        status='failed',
        error='provider unavailable',
        thinking='reasoning',
        tool_usage=OrchestrationToolUsage(
            state_changing_tools=('write_file',),
            exploratory_tools=('read_file',),
            reported=True,
        ),
    )


def test_typed_result_is_idempotent_and_none_keeps_legacy_empty_success():
    typed = OrchestrationAgentResult(output='done')

    assert normalize_orchestration_agent_result(typed) is typed
    assert normalize_orchestration_agent_result(None) == OrchestrationAgentResult()
    assert normalize_orchestration_agent_result({}) == OrchestrationAgentResult()


def test_invalid_top_level_shape_becomes_an_explicit_failed_node_result():
    result = normalize_orchestration_agent_result(['not', 'a', 'mapping'])

    assert result.status == 'failed'
    assert 'invalid agent runner result' in result.error
    assert result.output == ''


def test_invalid_custom_runner_result_reaches_canonical_node_failure_outcome():
    definition = {
        'schema': 'tofu.orchestration/v1',
        'name': 'Runner boundary',
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start', 'params': {}},
            {'id': 'w', 'type': 'role', 'role': 'worker', 'params': {}},
            {'id': 'e', 'type': 'control', 'kind': 'stop', 'params': {}},
        ],
        'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'e'}],
    }

    outcome = FlowExecutor(
        definition, agent_runner=lambda *_args: ['invalid'],
    ).run()

    assert outcome['ok'] is False
    assert outcome['stop_reason'] == 'node_failed'
    assert outcome['outcome']['lifecycle_status'] == 'error'
    assert 'invalid agent runner result' in outcome['outcome']['error']


def test_engine_consumes_only_the_normalized_runner_result_port():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (ROOT / 'lib' / 'orchestration_role_runtime.py').read_text()

    assert 'result = normalize_orchestration_agent_result(raw_result)' in runtime
    assert 'result.output' in runtime
    assert 'result.status' in runtime
    assert 'result.error' in runtime
    assert 'result.thinking' in runtime
    for field in ('output', 'status', 'error', 'thinking'):
        assert f"result.get('{field}')" not in runtime
    assert 'self._runner(' not in engine.split(
        '    def _run_role(', 1,
    )[1].split('    def _increment_agents_run(', 1)[0]

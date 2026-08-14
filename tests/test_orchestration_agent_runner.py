"""Production SubAgent adapter contract for the graph interpreter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.orchestration_agent_runner import (
    OrchestrationAgentRunnerConfig,
    OrchestrationSubAgentRunner,
)
from lib.orchestration_runner_result import OrchestrationAgentResult
from lib.orchestration_tool_usage import OrchestrationToolUsage


pytestmark = pytest.mark.unit


def test_config_replays_public_executor_options_for_isolated_children():
    parent = {'id': 'parent'}
    tools = [{'function': {'name': 'write_file'}}]
    config = OrchestrationAgentRunnerConfig(
        parent_task=parent,
        all_tools=tools,
        model='model-x',
        project_path='/workspace',
        system_prompt_base='project policy',
        thinking_enabled=False,
    )

    first = config.executor_options()
    second = config.executor_options()
    assert first == second
    assert first is not second
    assert first['parent_task'] is parent
    assert first['all_tools'] is tools
    assert first['model'] == 'model-x'
    assert first['project_path'] == '/workspace'
    assert first['system_prompt_base'] == 'project policy'
    assert first['thinking_enabled'] is False


def test_adapter_maps_node_config_streaming_and_result(monkeypatch):
    import lib.swarm.agent as agent_module
    import lib.swarm.protocol as protocol_module

    captured = {}

    class FakeSpec:
        def __init__(self, **kwargs):
            captured['spec'] = kwargs

    class FakeAgent:
        def __init__(self, spec, **kwargs):
            captured['agent'] = {'spec': spec, **kwargs}

        def run(self):
            sink = captured['agent']['stream_sink']
            sink('phase', 'waiting', phase='model', attempt=2)
            sink('thinking', 'reasoning ')
            sink('thinking', 'continues')
            sink('content', 'answer')
            return SimpleNamespace(
                final_answer='final answer',
                status='completed',
                error_message='must be suppressed',
                tool_log=[{'tool': 'write_file'}],
            )

    monkeypatch.setattr(protocol_module, 'SubTaskSpec', FakeSpec)
    monkeypatch.setattr(agent_module, 'SubAgent', FakeAgent)
    events = []
    abort = lambda: False
    parent = {'id': 'task'}
    tools = [{'function': {'name': 'write_file'}}]
    runner = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(
            parent_task=parent,
            all_tools=tools,
            model='m',
            project_path='/repo',
            system_prompt_base='system',
            thinking_enabled=True,
        ),
        emit=events.append,
        abort_check=abort,
    )

    result = runner({
        'id': 'worker-1',
        'type': 'role',
        'role': 'worker',
        'name': 'Worker',
        'params': {'objective': 'Ship it', 'tier': 'heavy'},
    }, 'upstream context', 3)

    assert captured['spec'] == {
        'role': 'worker',
        'objective': 'Ship it',
        'context': 'upstream context',
        'model_tier': 'heavy',
    }
    agent = captured['agent']
    assert agent['parent_task'] is parent
    assert agent['all_tools'] is tools
    assert agent['system_prompt_base'] == 'system'
    assert agent['model'] == 'm'
    assert agent['thinking_enabled'] is True
    assert agent['abort_check'] is abort
    assert agent['project_path'] == '/repo'
    assert events == [
        {
            'type': 'step_phase', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'phase': 'model', 'detail': 'waiting',
            'attempt': 2,
        },
        {
            'type': 'step_delta', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'kind': 'thinking',
            'chunk': 'reasoning ',
        },
        {
            'type': 'step_delta', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'kind': 'thinking', 'chunk': 'continues',
        },
        {
            'type': 'step_delta', 'node_id': 'worker-1', 'role': 'worker',
            'emits': 'assistant', 'kind': 'content', 'chunk': 'answer',
        },
    ]
    assert result == OrchestrationAgentResult(
        output='final answer',
        status='completed',
        error='',
        thinking='reasoning continues',
        tool_usage=OrchestrationToolUsage(
            state_changing_tools=('write_file',),
            reported=True,
        ),
    )


def test_adapter_preserves_subagent_failure(monkeypatch):
    import lib.swarm.agent as agent_module
    import lib.swarm.protocol as protocol_module

    monkeypatch.setattr(
        protocol_module, 'SubTaskSpec', lambda **kwargs: kwargs)

    class FailedAgent:
        def __init__(self, _spec, **_kwargs):
            pass

        def run(self):
            return SimpleNamespace(
                final_answer='partial',
                status='failed',
                error_message='provider unavailable',
                tool_log=None,
            )

    monkeypatch.setattr(agent_module, 'SubAgent', FailedAgent)
    runner = OrchestrationSubAgentRunner(
        OrchestrationAgentRunnerConfig(),
        emit=lambda _event: None,
        abort_check=lambda: False,
    )
    result = runner({
        'id': 'worker', 'type': 'role', 'role': 'worker', 'params': {},
    }, '', 0)

    assert result == OrchestrationAgentResult(
        output='partial',
        status='failed',
        error='provider unavailable',
        thinking='',
        tool_usage=OrchestrationToolUsage(reported=True),
    )


def test_engine_keeps_runner_patch_point_but_no_swarm_implementation():
    engine = open('lib/orchestration_engine.py', encoding='utf-8').read()
    adapter = open(
        'lib/orchestration_agent_runner.py', encoding='utf-8').read()

    assert 'def _default_runner(' in engine
    assert 'return self._default_runner_adapter(' in engine
    assert 'from lib.swarm.agent import SubAgent' not in engine
    assert 'from lib.swarm.protocol import' not in engine
    assert 'class OrchestrationSubAgentRunner' in adapter
    assert '-> OrchestrationAgentResult' in adapter
    assert 'from lib.swarm.agent import SubAgent' in adapter
    assert adapter.index('def __call__(') < adapter.index(
        'from lib.swarm.agent import SubAgent')
    assert engine.count('\n') < 1820
